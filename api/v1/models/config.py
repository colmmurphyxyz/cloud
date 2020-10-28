
import ipaddress

from pydantic import BaseModel
from pathlib import Path
from typing import Set, List, Dict, Optional, Tuple
from v1 import models

class Auth(BaseModel):
    direct_grant_url: str = "http://keycloak.netsoc.dev/auth/realms/freeipa/protocol/openid-connect/auth"

    class JWT(BaseModel):
        public_key: str

    jwt: JWT

class Accounts(BaseModel):
    home_dirs: Path
    username_blacklist: List[str]

    class FreeIPA(BaseModel):
        server: str
        username: str
        password: str

    freeipa: FreeIPA

class Email(BaseModel):
    from_name: str = "UCC Netsoc Admin"
    from_address: str = "netsocadmin@netsoc.co"
    reply_to_address: str = "netsoc@uccsocieties.ie"

    class SendGrid(BaseModel):
        key: str

    sendgrid: SendGrid

class Links(BaseModel):
    base_url: str = "https://admin.netsoc.co"
    
    class JWT(BaseModel):
        public_key: str
        private_key: str

    jwt: JWT

class Metrics(BaseModel):
    port: int = 8000

class Webhooks(BaseModel):
    form_filled: str
    info: str

class MkHomeDir(BaseModel):
    pass

class Captcha(BaseModel):
    class Hcaptcha(BaseModel):
        secret: Optional[str]
        url: str = "https://hcaptcha.com/siteverify"
    
    hcaptcha: Optional[Hcaptcha]
    enabled: bool = False

class HomeDirConsistency(BaseModel):
    scan_interval: int

from .proxmox import Template
class Proxmox(BaseModel):
    class SSH(BaseModel):
        server: str
        port: str
        username: str
        password: str

    class API(BaseModel):
        server: str
        port: str
        username: str
        password: str

    class VPS(BaseModel):
        network: ipaddress.IPv4Interface = ipaddress.IPv4Interface("10.69.0.0/16")
        bridge: str = "vmbr0"
        vlan: int = 69
        base_fqdn: str = "vps.cloud.netsoc.co"
        templates: Dict[str, Template] = {}
        dir_pool: str = "local"

        inactivity_shutdown_warning_num_days: int = 30
        inactivity_shutdown_num_days: int = 60
        inactivity_deletion_warning_num_days: int = 90
        inactivity_deletion_num_days: int = 120

    class LXC(BaseModel):
        network: ipaddress.IPv4Interface = ipaddress.IPv4Interface("10.68.0.0/16")
        bridge: str = "vmbr0"
        vlan: int = 68
        base_fqdn: str = "lxc.cloud.netsoc.co"
        templates: Dict[str, Template] = {}
        dir_pool: str = "local"

        inactivity_shutdown_warning_num_days: int = 60
        inactivity_shutdown_num_days: int = 90
        inactivity_deletion_warning_num_days: int = 120
        inactivity_deletion_num_days: int = 150

    class PortForward(BaseModel):
        range: Tuple[int,int] = (16384,32767)

    class VHosts(BaseModel):
        class UserSupplied(BaseModel):
            verification_txt_name: str = "_netsoc"
            allowed_a_aaaa: Set[str] = set(["84.39.234.52"])

        class NetsocSupplied(BaseModel):
            base_domain: str = "netsoc.co"

        netsoc_supplied: NetsocSupplied = NetsocSupplied()
        user_supplied: UserSupplied = UserSupplied()

    ssh: SSH
    api: API
    vps: VPS
    lxc: LXC
    port_forward: PortForward = PortForward()
    vhosts: VHosts = VHosts()

class Config(BaseModel):
    production: bool = False
    home_dirs: Path = Path("/home/users")
    accounts: Accounts
    auth: Auth
    email: Email
    links: Links
    webhooks: Webhooks
    homedir_consistency: HomeDirConsistency
    metrics: Metrics
    captcha: Optional[Captcha]
    proxmox: Proxmox
