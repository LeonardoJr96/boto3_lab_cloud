"""
Script de provisionamento de infraestrutura AWS usando boto3.

Cria (arquitetura equivalente ao Lab 6 da AWS Academy, com RDS Postgres):
  - 1 VPC, 2 AZs (subnet pública + privada em cada)
  - Internet Gateway (subnets públicas) + NAT Gateway (subnets privadas)
  - Route Tables (pública -> IGW, privada -> NAT)
  - Security Groups em cadeia: Internet -> ALB -> App(Nginx) -> RDS
  - Application Load Balancer nas subnets públicas
  - Launch Template + Auto Scaling Group (2-6 instâncias, scaling por CPU 60%)
    Cada instância roda backend FastAPI (systemd, 127.0.0.1:8000) e frontend
    React/Vite estático, servidos pelo Nginx na porta 80 (proxy de /api/*
    pro backend -- resolve CORS de graça, mesma origem).
  - RDS PostgreSQL Multi-AZ nas subnets privadas

Pré-requisitos:
  pip install boto3
  Credenciais via `aws configure` OU variáveis de ambiente:
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_SESSION_TOKEN=...   (Learning Lab expira em algumas horas --
                                     se der ExpiredToken, pegue credenciais
                                     novas na aba "AWS Details" > "AWS CLI")

Idempotente: pode rodar de novo que ele reaproveita o que já existe (acha
por tag "Name" / nome do recurso) em vez de duplicar.

Autor: Leonardo e Paulo
"""

import base64
import time

import boto3
from botocore.exceptions import ClientError, WaiterError

# ============================================================
# CONFIGURAÇÕES GERAIS — ajuste aqui antes de rodar
# ============================================================
REGION = "us-east-1"
VPC_CIDR = "10.0.0.0/16"

PUBLIC_SUBNET_CIDRS = ["10.0.1.0/24", "10.0.2.0/24"]
PRIVATE_SUBNET_CIDRS = ["10.0.11.0/24", "10.0.12.0/24"]

DB_NAME = "nexusdb"
DB_USER = "postgres"
DB_PASSWORD = "TrocarEssaSenha123!"  # NUNCA em produção -- use Secrets Manager
DB_INSTANCE_CLASS = "db.t3.micro"

# No AWS Academy Learning Lab, EC2/RDS só podem usar o LabInstanceProfile
# (pré-criado no lab -- não dá pra criar IAM roles novas).
EC2_INSTANCE_PROFILE = "LabInstanceProfile"
EC2_INSTANCE_TYPE = "t3.small"
EC2_AMI_ID = "ami-0453ec754f44f9a4a"  # Amazon Linux 2023, us-east-1

# Sem key pair não dá pra entrar via SSH pra debugar (/var/log/user-data.log,
# journalctl -u backend). Recomendado criar uma no console EC2.
EC2_KEY_NAME = None

REPO_BACKEND_URL = "https://github.com/LeonardoJr96/backend_to_do_list.git"
REPO_FRONTEND_URL = "https://github.com/LeonardoJr96/frontend_to_do_list.git"

# Porta liberada pelo ALB/SG -- é 80 porque o Nginx no host recebe o
# tráfego e faz proxy pro backend em 127.0.0.1:8000 (nunca exposto direto).
EC2_APP_PORT = 80

PROJECT_NAME = "lab-aws-cloud"

# Credenciais vêm do ambiente / `aws configure` -- nunca hardcoded aqui.
session = boto3.Session(region_name=REGION)
ec2 = session.client("ec2")
elbv2 = session.client("elbv2")
rds = session.client("rds")
autoscaling = session.client("autoscaling")


def tag(resource_id, name):
    ec2.create_tags(Resources=[resource_id], Tags=[{"Key": "Name", "Value": name}])


def get_or_create(description, describe_fn, create_fn):
    """
    Helper genérico de idempotência: tenta achar o recurso pelo describe_fn;
    se não existir, cria com create_fn. Evita duplicar VPC, subnet, SG, LB,
    TG, LT etc. toda vez que o script roda de novo.
    """
    existing = describe_fn()
    if existing:
        print(f"{description} já existe.")
        return existing
    print(f"Criando {description}...")
    created = create_fn()
    print(f"{description} criado.")
    return created


def safe_authorize_ingress(group_id, ip_permissions):
    try:
        ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=ip_permissions)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise


def find_by_name_tag(describe_call, key, resources_key, name):
    """Busca genérica por tag Name=<name> em describe_vpcs/subnets/route_tables/security_groups."""
    response = describe_call(Filters=[{"Name": "tag:Name", "Values": [name]}])
    items = response.get(resources_key, [])
    return items[0] if items else None


# ============================================================
# 1. VPC
# ============================================================
def create_vpc():
    def describe():
        return find_by_name_tag(ec2.describe_vpcs, "tag:Name", "Vpcs", f"{PROJECT_NAME}-vpc")

    def create():
        vpc = ec2.create_vpc(CidrBlock=VPC_CIDR)["Vpc"]
        ec2.get_waiter("vpc_available").wait(VpcIds=[vpc["VpcId"]])
        tag(vpc["VpcId"], f"{PROJECT_NAME}-vpc")
        # RDS exige DNS habilitado na VPC pra resolver o endpoint do banco
        ec2.modify_vpc_attribute(VpcId=vpc["VpcId"], EnableDnsSupport={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc["VpcId"], EnableDnsHostnames={"Value": True})
        return vpc

    return get_or_create("VPC", describe, create)["VpcId"]


def get_azs():
    azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
    return [az["ZoneName"] for az in azs["AvailabilityZones"][:2]]


# ============================================================
# 2. Subnets — uma pública + uma privada por AZ
# ============================================================
def create_subnet(vpc_id, cidr, az, name, public):
    def describe():
        response = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "tag:Name", "Values": [name]}])
        subnets = response.get("Subnets", [])
        return subnets[0] if subnets else None

    def create():
        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az)["Subnet"]
        if public:
            ec2.modify_subnet_attribute(SubnetId=subnet["SubnetId"], MapPublicIpOnLaunch={"Value": True})
        tag(subnet["SubnetId"], name)
        return subnet

    return get_or_create(f"Subnet {name}", describe, create)["SubnetId"]


def create_subnets(vpc_id, azs):
    public_subnets = [
        create_subnet(vpc_id, PUBLIC_SUBNET_CIDRS[i], az, f"{PROJECT_NAME}-public-{az}", public=True)
        for i, az in enumerate(azs)
    ]
    private_subnets = [
        create_subnet(vpc_id, PRIVATE_SUBNET_CIDRS[i], az, f"{PROJECT_NAME}-private-{az}", public=False)
        for i, az in enumerate(azs)
    ]
    print(f"Subnets públicas: {public_subnets} | privadas: {private_subnets}")
    return public_subnets, private_subnets


# ============================================================
# 3. Internet Gateway + NAT Gateway
# ============================================================
def create_igw(vpc_id):
    def describe():
        igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])["InternetGateways"]
        return igws[0] if igws else None

    def create():
        igw = ec2.create_internet_gateway()["InternetGateway"]
        ec2.attach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
        tag(igw["InternetGatewayId"], f"{PROJECT_NAME}-igw")
        return igw

    return get_or_create("Internet Gateway", describe, create)["InternetGatewayId"]


def create_nat_gateway(public_subnet_id):
    def describe():
        nats = ec2.describe_nat_gateways(Filter=[{"Name": "tag:Name", "Values": [f"{PROJECT_NAME}-nat"]}])["NatGateways"]
        return next((n for n in nats if n["State"] in {"available", "pending"}), None)

    def create():
        eip = ec2.allocate_address(Domain="vpc")
        nat = ec2.create_nat_gateway(SubnetId=public_subnet_id, AllocationId=eip["AllocationId"])["NatGateway"]
        print("Aguardando NAT Gateway ficar disponível (leva alguns minutos)...")
        ec2.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat["NatGatewayId"]])
        tag(nat["NatGatewayId"], f"{PROJECT_NAME}-nat")
        return nat

    nat = get_or_create("NAT Gateway", describe, create)
    if nat["State"] != "available":
        ec2.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat["NatGatewayId"]])
    return nat["NatGatewayId"]


# ============================================================
# 4. Route Tables
# ============================================================
def setup_route_table(vpc_id, name, subnets, destination, gateway_id=None, nat_gateway_id=None):
    def describe():
        return find_by_name_tag(ec2.describe_route_tables, "tag:Name", "RouteTables", name)

    def create():
        rt = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]
        tag(rt["RouteTableId"], name)
        return rt

    rt_id = get_or_create(f"Route table {name}", describe, create)["RouteTableId"]

    routes = ec2.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]["Routes"]
    if not any(r.get("DestinationCidrBlock") == destination for r in routes):
        params = {"RouteTableId": rt_id, "DestinationCidrBlock": destination}
        params.update({"GatewayId": gateway_id} if gateway_id else {"NatGatewayId": nat_gateway_id})
        ec2.create_route(**params)

    associated = {a.get("SubnetId") for a in ec2.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]["Associations"]}
    for subnet_id in subnets:
        if subnet_id not in associated:
            ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)

    return rt_id


def create_route_tables(vpc_id, igw_id, nat_id, public_subnets, private_subnets):
    setup_route_table(vpc_id, f"{PROJECT_NAME}-public-rt", public_subnets, "0.0.0.0/0", gateway_id=igw_id)
    setup_route_table(vpc_id, f"{PROJECT_NAME}-private-rt", private_subnets, "0.0.0.0/0", nat_gateway_id=nat_id)
    print("Route tables configuradas.")


# ============================================================
# 5. Security Groups — cadeia: Internet -> ALB -> App -> RDS
# ============================================================
def create_security_group(vpc_id, name, description):
    def describe():
        return find_by_name_tag(ec2.describe_security_groups, "tag:Name", "SecurityGroups", name)

    def create():
        sg = ec2.create_security_group(GroupName=name, Description=description, VpcId=vpc_id)
        tag(sg["GroupId"], name)
        return sg

    return get_or_create(f"Security Group {name}", describe, create)["GroupId"]


def create_security_groups(vpc_id):
    alb_sg = create_security_group(vpc_id, f"{PROJECT_NAME}-alb-sg", "Trafego web para o ALB")
    safe_authorize_ingress(alb_sg, [
        {"IpProtocol": "tcp", "FromPort": p, "ToPort": p, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
        for p in (80, 443)
    ])

    app_sg = create_security_group(vpc_id, f"{PROJECT_NAME}-app-sg", "Trafego apenas vindo do ALB")
    safe_authorize_ingress(app_sg, [
        {"IpProtocol": "tcp", "FromPort": EC2_APP_PORT, "ToPort": EC2_APP_PORT,
         "UserIdGroupPairs": [{"GroupId": alb_sg}]},
    ])
    if EC2_KEY_NAME:
        safe_authorize_ingress(app_sg, [
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        ])

    rds_sg = create_security_group(vpc_id, f"{PROJECT_NAME}-rds-sg", "Postgres apenas para a aplicacao")
    safe_authorize_ingress(rds_sg, [
        {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "UserIdGroupPairs": [{"GroupId": app_sg}]},
    ])

    print(f"Security Groups -> ALB: {alb_sg} | App: {app_sg} | RDS: {rds_sg}")
    return alb_sg, app_sg, rds_sg


# ============================================================
# 6. Application Load Balancer
# ============================================================
def create_load_balancer(public_subnets, alb_sg, vpc_id):
    def describe_lb():
        try:
            return elbv2.describe_load_balancers(Names=[f"{PROJECT_NAME}-alb"])["LoadBalancers"][0]
        except ClientError:
            return None

    def create_lb():
        return elbv2.create_load_balancer(
            Name=f"{PROJECT_NAME}-alb", Subnets=public_subnets, SecurityGroups=[alb_sg],
            Scheme="internet-facing", Type="application", IpAddressType="ipv4",
        )["LoadBalancers"][0]

    lb_arn = get_or_create("Load Balancer", describe_lb, create_lb)["LoadBalancerArn"]

    def describe_tg():
        try:
            return elbv2.describe_target_groups(Names=[f"{PROJECT_NAME}-tg"])["TargetGroups"][0]
        except ClientError:
            return None

    def create_tg():
        return elbv2.create_target_group(
            Name=f"{PROJECT_NAME}-tg", Protocol="HTTP", Port=EC2_APP_PORT, VpcId=vpc_id,
            TargetType="instance", HealthCheckPath="/health", HealthCheckIntervalSeconds=30,
            HealthCheckTimeoutSeconds=10, HealthyThresholdCount=2, UnhealthyThresholdCount=5,
        )["TargetGroups"][0]

    tg_arn = get_or_create("Target Group", describe_tg, create_tg)["TargetGroupArn"]

    listeners = elbv2.describe_listeners(LoadBalancerArn=lb_arn)["Listeners"]
    if not any(l["Port"] == 80 for l in listeners):
        elbv2.create_listener(
            LoadBalancerArn=lb_arn, Protocol="HTTP", Port=80,
            DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )

    lb_dns = elbv2.describe_load_balancers(LoadBalancerArns=[lb_arn])["LoadBalancers"][0]["DNSName"]
    print(f"Load Balancer preparado: {lb_dns}")
    return lb_arn, tg_arn


# ============================================================
# 7. User data (bootstrap de cada instância)
# ============================================================
def user_data_script(db_endpoint):
    """
    Roda no boot de cada instância do Auto Scaling:
      0. Loga tudo em /var/log/user-data.log (set -x) -- essencial pra
         debugar 502 (causa mais comum: backend não sobe e Nginx nunca é
         configurado, por causa do set -e).
      1. Instala Python/Git/Node 20/Nginx + libs de build do psycopg2.
      2. Cria swapfile (build do Vite pode apertar a memória do t3.small).
      3. Clona e sobe o backend FastAPI via systemd em 127.0.0.1:8000,
         nunca exposto direto -- credencial do banco via EnvironmentFile
         com permissão 600 (não em texto puro no unit file, que é 644).
      4. Clona e builda o frontend estático (VITE_API_URL=/api).
      5. Nginx: serve o front com fallback SPA pra /index.html, /health
         direto (sem depender do backend) e proxy dedicado de /api/* pro
         backend.
    """
    db_url = f"postgresql://{DB_USER}:{DB_PASSWORD}@{db_endpoint}:5432/{DB_NAME}"

    return f"""#!/bin/bash
exec > >(tee /var/log/user-data.log) 2>&1
set -x
set -e

retry() {{
  local n=0 max=5
  until "$@"; do
    n=$((n+1))
    [ "$n" -ge "$max" ] && {{ echo "Comando falhou depois de $max tentativas: $*"; return 1; }}
    echo "Tentativa $n falhou, tentando de novo em 10s: $*"
    sleep 10
  done
}}

dnf update -y
dnf install -y python3 python3-pip python3-devel gcc git nginx libpq-devel
retry curl -fsSL https://rpm.nodesource.com/setup_20.x -o /tmp/nodesource_setup.sh
bash /tmp/nodesource_setup.sh
dnf install -y nodejs

# Swap: evita OOM durante o npm run build
dd if=/dev/zero of=/swapfile bs=128M count=16
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile

# Nginx sobe cedo servindo uma pagina de "preparando" + /health, pro ALB
# ja ter o que responder enquanto backend/frontend ainda instalam.
mkdir -p /var/www/frontend
cat > /var/www/frontend/index.html << 'HTMLEOF'
<html><body><h1>Preparando aplicacao, aguarde...</h1></body></html>
HTMLEOF
cat > /etc/nginx/conf.d/todo.conf << 'NGINXEOF'
server {{
    listen 80;
    root /var/www/frontend;
    index index.html;

    location /health {{
        default_type text/plain;
        return 200 "starting";
    }}

    location / {{
        try_files $uri $uri/ /index.html;
    }}
}}
NGINXEOF
rm -f /etc/nginx/conf.d/default.conf
systemctl enable --now nginx

# Backend
cd /home/ec2-user
retry git clone {REPO_BACKEND_URL} backend
cd backend
retry python3 -m pip install poetry
python3 -m pip install psycopg2-binary
poetry install --only main

cat > /home/ec2-user/start_backend.sh << 'STARTEOF'
#!/bin/bash
set -e
cd /home/ec2-user/backend
if [ -f app/main.py ]; then APP_MODULE="app.main:app"; else APP_MODULE="main:app"; fi
for i in $(seq 1 24); do
  if python3 - <<'PY'
import os, socket
url = os.environ.get("DATABASE_URL", "")
if not url:
    raise SystemExit(1)
try:
    socket.create_connection((url.split("@")[-1].split(":")[0], 5432), timeout=2)
    raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PY
  then break; fi
  echo "Aguardando RDS ficar acessivel... tentativa $i/24"
  sleep 10
done
exec /usr/bin/python3 -m uvicorn "$APP_MODULE" --host 127.0.0.1 --port 8000
STARTEOF
chmod +x /home/ec2-user/start_backend.sh

umask 077
cat > /etc/backend.env << 'ENVEOF'
DATABASE_URL={db_url}
ENVEOF
chmod 600 /etc/backend.env
chown root:root /etc/backend.env

cat > /etc/systemd/system/backend.service << 'SERVICEEOF'
[Unit]
Description=FastAPI Backend
After=network.target

[Service]
WorkingDirectory=/home/ec2-user/backend
EnvironmentFile=/etc/backend.env
ExecStart=/home/ec2-user/start_backend.sh
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF
systemctl daemon-reload
systemctl enable --now backend

# Frontend
cd /home/ec2-user
retry git clone {REPO_FRONTEND_URL} frontend
cd frontend
echo "VITE_API_URL=/api" > .env.production
retry npm ci || retry npm install
npm run build
rm -rf /var/www/frontend/*
cp -r dist/* /var/www/frontend/

# Nginx final: /health local, /api/* proxy dedicado, resto cai no SPA
cat > /etc/nginx/conf.d/todo.conf << 'NGINXEOF'
server {{
    listen 80;
    root /var/www/frontend;
    index index.html;

    location /health {{
        default_type text/plain;
        return 200 "ok";
    }}

    location /api/ {{
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 5s;
    }}

    location / {{
        try_files $uri $uri/ /index.html;
    }}
}}
NGINXEOF
systemctl restart nginx

# Espera o backend responder de verdade (ate ~6min: RDS + build)
ok=0
for i in $(seq 1 36); do
  curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && {{ ok=1; break; }}
  sleep 10
done

if [ "$ok" -eq 1 ]; then
  echo "Backend respondendo. Bootstrap concluido com sucesso."
else
  echo "AVISO: backend nao respondeu a tempo. Diagnostico:"
  systemctl status backend --no-pager || true
  journalctl -u backend -n 80 --no-pager || true
fi
"""


# ============================================================
# 8. Launch Template + Auto Scaling Group
# ============================================================
def create_launch_template(app_sg, db_endpoint):
    def describe():
        try:
            return ec2.describe_launch_templates(LaunchTemplateNames=[f"{PROJECT_NAME}-lt"])["LaunchTemplates"][0]
        except ClientError:
            return None

    def create():
        lt_data = {
            "ImageId": EC2_AMI_ID,
            "InstanceType": EC2_INSTANCE_TYPE,
            "SecurityGroupIds": [app_sg],
            "IamInstanceProfile": {"Name": EC2_INSTANCE_PROFILE},
            "UserData": base64.b64encode(user_data_script(db_endpoint).encode()).decode(),
            "TagSpecifications": [{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": f"{PROJECT_NAME}-web"}]}],
        }
        if EC2_KEY_NAME:
            lt_data["KeyName"] = EC2_KEY_NAME
        return ec2.create_launch_template(LaunchTemplateName=f"{PROJECT_NAME}-lt", LaunchTemplateData=lt_data)["LaunchTemplate"]

    return get_or_create("Launch Template", describe, create)["LaunchTemplateId"]


def create_auto_scaling_group(lt_id, public_subnets, tg_arn):
    existing = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[f"{PROJECT_NAME}-asg"])["AutoScalingGroups"]
    if existing:
        print(f"Auto Scaling Group já existe: {existing[0]['AutoScalingGroupName']}")
        return

    azs = list({s["AvailabilityZone"] for s in ec2.describe_subnets(SubnetIds=public_subnets)["Subnets"]})

    autoscaling.create_auto_scaling_group(
        AutoScalingGroupName=f"{PROJECT_NAME}-asg",
        LaunchTemplate={"LaunchTemplateId": lt_id, "Version": "$Latest"},
        MinSize=2, MaxSize=6, DesiredCapacity=2,
        VPCZoneIdentifier=",".join(public_subnets),
        AvailabilityZones=azs,
        TargetGroupARNs=[tg_arn],
        HealthCheckType="ELB",
        # Grace period generoso: da tempo do bootstrap (clone + build +
        # espera pelo RDS) terminar antes do ALB reciclar por "unhealthy".
        HealthCheckGracePeriod=420,
        Tags=[{"Key": "Name", "Value": f"{PROJECT_NAME}-web", "PropagateAtLaunch": True}],
    )
    autoscaling.put_scaling_policy(
        AutoScalingGroupName=f"{PROJECT_NAME}-asg",
        PolicyName=f"{PROJECT_NAME}-scaling-policy",
        PolicyType="TargetTrackingScaling",
        TargetTrackingConfiguration={
            "PredefinedMetricSpecification": {"PredefinedMetricType": "ASGAverageCPUUtilization"},
            "TargetValue": 60.0,
        },
    )
    print("Auto Scaling Group criado (2-6 instâncias, CPU alvo 60%).")


# ============================================================
# 9. RDS PostgreSQL — Multi-AZ, subnets privadas
# ============================================================
def create_rds(private_subnets, rds_sg):
    subnet_group_name = f"{PROJECT_NAME}-db-subnet-group"
    db_identifier = f"{PROJECT_NAME}-db"

    try:
        rds.create_db_subnet_group(
            DBSubnetGroupName=subnet_group_name,
            DBSubnetGroupDescription="Subnets privadas para o RDS",
            SubnetIds=private_subnets,
        )
        print(f"DB subnet group criado: {subnet_group_name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "DBSubnetGroupAlreadyExists":
            raise
        print(f"DB subnet group já existe: {subnet_group_name}")

    try:
        rds.describe_db_instances(DBInstanceIdentifier=db_identifier)
        print(f"Instância RDS já existe: {db_identifier}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"DBInstanceNotFound", "DBInstanceNotFoundFault"}:
            raise
        print("Criando instância RDS Multi-AZ (leva de 5 a 10 minutos)...")
        rds.create_db_instance(
            DBInstanceIdentifier=db_identifier,
            DBName=DB_NAME,
            Engine="postgres",
            MasterUsername=DB_USER,
            MasterUserPassword=DB_PASSWORD,
            DBInstanceClass=DB_INSTANCE_CLASS,
            AllocatedStorage=20,
            VpcSecurityGroupIds=[rds_sg],
            DBSubnetGroupName=subnet_group_name,
            MultiAZ=True,              # réplica síncrona automática na 2ª AZ
            PubliclyAccessible=False,  # só acessível de dentro da VPC
            BackupRetentionPeriod=7,
            StorageEncrypted=True,
        )

    rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=db_identifier)
    endpoint = rds.describe_db_instances(DBInstanceIdentifier=db_identifier)["DBInstances"][0]["Endpoint"]["Address"]
    print(f"RDS disponível em: {endpoint}")
    return endpoint


# ============================================================
# EXECUÇÃO — a ordem importa (cada recurso depende do anterior)
# ============================================================
if __name__ == "__main__":
    vpc_id = create_vpc()
    azs = get_azs()
    public_subnets, private_subnets = create_subnets(vpc_id, azs)
    igw_id = create_igw(vpc_id)
    nat_id = create_nat_gateway(public_subnets[0])
    create_route_tables(vpc_id, igw_id, nat_id, public_subnets, private_subnets)
    alb_sg, app_sg, rds_sg = create_security_groups(vpc_id)
    lb_arn, tg_arn = create_load_balancer(public_subnets, alb_sg, vpc_id)

    # RDS demora 5-10min -- criamos antes do ASG pra já nascer sabendo o
    # endpoint do banco (fica fixo no backend do app via user-data).
    db_endpoint = create_rds(private_subnets, rds_sg)

    lt_id = create_launch_template(app_sg, db_endpoint)
    create_auto_scaling_group(lt_id, public_subnets, tg_arn)

    lb_dns = elbv2.describe_load_balancers(LoadBalancerArns=[lb_arn])["LoadBalancers"][0]["DNSName"]

    print("\n=== INFRAESTRUTURA CRIADA COM SUCESSO ===")
    print(f"VPC ID:      {vpc_id}")
    print(f"DB Endpoint: {db_endpoint}")
    print(f"URL do App:  http://{lb_dns}")
    print("\nO Auto Scaling Group leva alguns minutos pra subir as instâncias")
    print("(clone + build + espera pelo RDS). /health responde 'starting' assim")
    print("que o Nginx sobe, e 'ok' quando a config final é aplicada.")
    print("Se travar em 'starting' por muito tempo, via SSH (se EC2_KEY_NAME")
    print("estiver configurado): cat /var/log/user-data.log")
    print("                       journalctl -u backend -n 100 --no-pager")