
import random
import yaml
import datetime
import requests
import hashlib
import os
import socket
import shutil
import paramiko
import structlog as logging

from urllib.parse import urlparse, unquote

from typing import Optional, BinaryIO
from proxmoxer import ProxmoxAPI

from v1 import models, exceptions, utilities
from v1.config import config

logger = logging.getLogger(__name__)

class ProxmoxNodeSSH:
    """Paramiko SSH client that will first SSH into an exposed Proxmox node, then jump into any of the nodes in the Cluster"""

    jump: paramiko.SSHClient
    node: paramiko.SSHClient
    node_name: str

    ssh: paramiko.SSHClient
    sftp: paramiko.SFTPClient

    def __init__(self, node_name: str):
        self.jump = paramiko.SSHClient()
        self.node = paramiko.SSHClient()
        self.node_name = node_name

    def __enter__(self):
        self.jump.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        self.jump.connect(
            hostname=config.proxmox.ssh.server,
            username=config.proxmox.ssh.username,
            password=config.proxmox.ssh.password,
            port=config.proxmox.ssh.port
        )

        self.jump_transport = self.jump.get_transport()
        self.jump_channel = self.jump_transport.open_channel("direct-tcpip", (self.node_name, 22), ("127.0.0.1", 22))

        self.node.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.node.connect(
            self.node_name, # nodes should be setup in /etc/hosts correctly
            username=config.proxmox.ssh.username,
            password=config.proxmox.ssh.password,
            port=22,
            sock=self.jump_channel
        )

        self.ssh = self.node
        self.sftp = self.ssh.open_sftp()

        return self

    def __exit__(self, type, value, traceback):
        self.node.close()
        self.jump.close()

class Proxmox():
    def __init__(self):
        self.prox = ProxmoxAPI(
            host=config.proxmox.api.server,
            user=config.proxmox.api.username,
            password=config.proxmox.api.password,
            port=config.proxmox.api.port,
            verify_ssl=False
        )

    def _select_best_node(
        self,
        specs: models.uservm.Specs
    ) -> Optional[str]:
        """Finds a good node based off specs given"""
        nodes = self.prox.nodes.get()

        if len(nodes) == 0:
            return None
        
        choice = nodes[0]["node"]

        scoreboard = {}
        for node in nodes:
            node_name = node['node']
            
            if node_name not in scoreboard:
                scoreboard[node_name] = 0

            if (node['maxmem'] - node['mem']) > (specs.memory*1000000):
                scoreboard[node_name] += 1

            if (node['mem']/node['maxmem']) < 0.6:
                scoreboard[node_name] += 1
            
            if (node['maxcpu'] >= specs.cores):
                scoreboard[node_name] += 1

        # return the node with the highest score
        return sorted(scoreboard, key=scoreboard.get, reverse=True)[0]

    def _get_fqdn_for_username(
        self,
        username: str,
        hostname: str
    ) -> str:
        return f"{hostname}.{username}.{models.uservm.fqdn}"

    def _get_fqdn_for_account(
        self,
        account: models.account.Account,
        hostname: str
    ) -> str:
        """
        Returns a FQDN for a VM hostname for an account

        i.e cake => cake.ocanty.uservm.netsoc.co
        """
        return self._get_fqdn_for_username(account.username, hostname)

    def _read_uservm_on_node(
        self,
        node: str,
        fqdn: str
    ) -> models.uservm.UserVM:  
        """Find a VM on a node or throw an exception"""

        # get each vm on the node
        for vm_dict in self.prox.nodes(node).qemu.get():
            if vm_dict['name'] == fqdn:
                config = self.prox.nodes(node).qemu.get(f"{vm_dict['vmid']}/config")

                # decode description
                try:
                    logger.info(yaml.safe_load(config['description']))
                    metadata = models.uservm.Metadata.parse_obj(
                        yaml.safe_load(config['description'])
                    )
                except Exception as e:
                    raise exceptions.resource.Unavailable(
                        f"User VM is unavailable, malformed metadata description: {e}"
                    )

                # for user ocanty, returns .ocanty.uservm.netsoc.co
                suffix = self._get_fqdn_for_username(metadata.owner, "")

                # trim suffix
                if not vm_dict['name'].endswith(suffix):
                    raise exceptions.resource.Unavailable(
                        f"Found VM but Owner / FQDN do not align, FQDN is {vm_dict['name']} but owner is {metadata.owner}"
                    )

                hostname = vm_dict['name'][:-len(suffix)]

                vm = models.uservm.UserVM(
                    id=vm_dict['vmid'],
                    fqdn=fqdn,
                    hostname=hostname,
                    node=node,
                    metadata=metadata,
                    specs=metadata.provision.image.specs,
                    remarks=[],
                    status=models.uservm.Status.NotApplicable
                )

                # if it's a vm request
                if metadata.provision.stage == models.uservm.Provision.Stage.AwaitingApproval:
                    vm.remarks.append(
                        "Your VM is currently awaiting approval from the SysAdmins, you will receive an email once it has been approved"
                    )
                elif metadata.provision.stage == models.uservm.Provision.Stage.Approved:
                    vm.remarks.append(
                        "Your VM has been approved, you can now install it"
                    )
                elif metadata.provision.stage == models.uservm.Provision.Stage.Installed:
                    vm.specs=models.uservm.Specs(
                        cores=config['cores'],
                        memory=config['memory'],
                        disk_space=0
                    ),

                    if 'virtio0' not in config:
                        vm.remarks.append("VM is missing its disk. Contact SysAdmins")
                    else:
                        # config str looks like this: local-lvm:vm-1867516-disk-0,backup=0,size=30G

                        # extract the size str
                        size_str = dict(map(lambda x: (x.split('=') + [""])[:2], config['virtio0'].split(',')))['size']

                        vm.specs.disk_space = int(size_str[:-1])
                else:
                    vm.remarks.append(
                        "Installation may take awhile, check back later"
                    )

                return vm

        raise exceptions.resource.NotFound("The VM does not exist")

    def _read_uservm_by_fqdn(
        self,
        fqdn: str
    ) -> models.uservm.UserVM:
        """Find a VM in the cluster or throw an exception"""

        # Look on each node for the VM
        for node in self.prox.nodes().get():
            try:
                vm = self._read_uservm_on_node(node['node'], fqdn)
                return vm
            except exceptions.resource.NotFound:
                pass
        
        raise exceptions.resource.NotFound("The VM does not exist")

    def read_by_account(
        self,
        account: models.account.Account,
        hostname: str
    ) -> models.uservm.UserVM:
        """Read a VM owned by an account"""
        return self._read_uservm_by_fqdn(self._get_fqdn_for_account(account, hostname))

    def request_vm(
        self,
        account: models.account.Account,
        hostname: str,
        image: models.uservm.Image,
        reason: str
    ):
        try:
            existing_vm = self.read_by_account(account, hostname)
            raise exceptions.resource.AlreadyExists(f"VM {hostname} already exists")
        except exceptions.resource.NotFound as e:
            pass

        node_name = self._select_best_node(image.specs)
        fqdn = self._get_fqdn_for_account(account, hostname)

        metadata = models.uservm.Metadata(
            owner=account.username,
            provision=models.uservm.Provision(
                image=image
            ),
            reason=reason,
            inactivity=models.uservm.Inactivity(
                last_marked_active=datetime.date.today()
            )
        )

        # Store VM metadata in the description field
        # https://github.com/samuelcolvin/pydantic/issues/1043
        yaml_description = yaml.dump(yaml.safe_load(metadata.json()), default_flow_style=False)

        # reserve a VM, but don't set any of the specs yet
        # just setup the metadata we'll need later
        random.seed(f"{fqdn}-{node_name}")
        vm_hash_id = random.randint(1000, 5000000)
        random.seed()
        
        self.prox.nodes(f"{node_name}/qemu").post(**{
            'name': fqdn,
            'vmid': vm_hash_id,
            'description': yaml_description
        })

    def write_out_metadata(
        self,
        vm: models.uservm.UserVM
    ):
        yaml_description = yaml.dump(yaml.safe_load(vm.metadata.json()), default_flow_style=False)
        
        # https://github.com/samuelcolvin/pydantic/issues/1043
        self.prox.nodes(vm.node).qemu(f"{vm.id}/config").post(description=yaml_description)

    def approve_vm(
        self,
        vm: models.uservm.UserVM
    ):  
        if vm.metadata.provision.stage == models.uservm.Provision.Stage.AwaitingApproval:
            vm.metadata.provision.stage = models.uservm.Provision.Stage.Approved

            self.write_out_metadata(vm)
    
    def uninstall_vm(
        self,
        vm: models.uservm.UserVM
    ):
        pass

    def install_vm(
        self,
        vm: models.uservm.UserVM
    ):
        # Don't allow installations on unapproved VMs
        if vm.metadata.provision.stage == models.uservm.Provision.Stage.AwaitingApproval:
            raise exceptions.resource.Unavailable("Cannot install VM. VM requires approval")
        
        # # Approved is the base "approved but not installed" state
        if vm.metadata.provision.stage != models.uservm.Provision.Stage.Approved:
            raise exceptions.resource.Unavailable("VM is currently installing/already installed")

        def provision_stage(stage: models.uservm.Provision.Stage):
            vm.metadata.provision.stage = stage
            vm.metadata.provision.remarks = []
            self.write_out_metadata(vm)

        def provision_remark(line: str):
            vm.metadata.provision.remarks.append(line)

        provision_stage(models.uservm.Provision.Stage.DownloadImage)

        try:
            with ProxmoxNodeSSH(vm.node) as con:
                stdin, stdout, stderr = con.ssh.exec_command("hostname")
                for line in stdout.read().split(b'\n'):
                    logger.info(str(line))
        except Exception as e:
            provision_stage(models.uservm.Provision.Stage.Failed)
            provision_remark(f"Could not download image: {e}")
                
            raise exceptions.resource.Unavailable(f"VM {vm.hostname} installation failed, check remarks")

        
        # # Strip url down to the filename
        # filename = os.path.basename(
        #     unquote(
        #         urlparse(vm.metadata.provision.image.disk_url).path
        #     ).split('?')[0]
        # )

        # # This happens when they submit a link that's like somelink.com/vmdisk1/
        # # i.e has no basepath
        # if filename == "":
        #     filename = f"{hashlib.sha256(vm.metadata.provision.image.disk_url).hexdigest()}.{vm.metadata.provision.image.disk_format}"

        # image_path = Path(f"/tmp/{filename}")
        
        # # First check if it's already been downloaded
        # if not image_path.exists() or utilities.hash.file_sha256(image_path) != vm.metadata.provision.image.disk_sha256:
        #     # Gotta download to a unique filename to ensure we don't conflict with the other workers/containers
        #     download_path = Path(f"/tmp/{socket.gethostname()}-{os.getpid()}-{filename}")

        #     with requests.get(vm.metadata.provision.image.disk_url, stream=True) as r:
        #         r.raw.decode_content = True

        #         with open(download_path, 'wb') as f:
        #             shutil.copyfileobj(r.raw, f)

        #     os.fsync()

        #     if utilities.hash.file_sha256(download_path) != vm.metadata.provision.image.disk_sha256:
        #         provision_stage(models.uservm.Provision.Stage.Failed)
        #         provision_remark(f"Downloaded image does not match SHA256 hash {vm.metadata.provision.image.disk_sha256}")
                
        #         raise exceptions.resource.Unavailable("Installation failed, check remarks")


        #     os.replace(download_path, image_path)
  
        # # Push image to proxmox host
        # provision_stage(models.uservm.Provision.Stage.PushImage)
        # provision_remark(f"Downloaded image does not match SHA256 hash {vm.metadata.provision.image.disk_sha256}")

        # local_storage = proxmox.nodes(vm.node).storage('local')
        # local_storage.upload.create(content='vztmpl',
        #     filename=open(image_path))
            

        # # 
        # # local_storage.upload.create(
        # #     content="iso",
        # #     filename=open(os.path.expanduser('~/templates/debian-6-my-core_1.0-1_i386.tar.gz')))
        # # )
            
