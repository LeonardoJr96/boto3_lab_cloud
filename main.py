"""
Script de provisionamento de infraestrutura AWS usando boto3.

Cria (arquitetura equivalente ao Lab 6 da AWS Academy, com RDS Postgres):
  - 1 VPC
  - 2 Availability Zones, cada uma com:
      - 1 subnet pública (IP público automático)
      - 1 subnet privada (sem IP público direto)
  - Internet Gateway (para as subnets públicas)
  - NAT Gateway (para as subnets privadas saírem pra internet)
  - Route Tables (pública -> IGW, privada -> NAT)
  - Security Groups em cadeia: Internet -> ALB -> App(Nginx) -> RDS
  - Application Load Balancer (nas subnets públicas)
  - Launch Template + Auto Scaling Group (2-6 instâncias, scaling por CPU 60%)
    Cada instância roda:
      - Backend FastAPI em container Docker (127.0.0.1:8000, isolado)
      - Frontend React/Vite buildado como estático
      - Nginx servindo o front na porta 80 e fazendo proxy reverso de
        /api/* pro backend (resolve CORS de graça, mesma origem)
  - RDS PostgreSQL Multi-AZ (nas subnets privadas)
  - DATABASE_URL do RDS fica embutida no backend (não é pedida ao usuário)

Pré-requisitos:
  pip install boto3
  Credenciais configuradas via `aws configure` ou variáveis de ambiente.

Autor: Leonardo e Paulo
"""

import boto3
import time

# ============================================================
# CONFIGURAÇÕES GERAIS — ajuste aqui antes de rodar
# ============================================================
REGION = "us-east-1"
VPC_CIDR = "10.0.0.0/16"

PUBLIC_SUBNET_CIDRS = ["10.0.1.0/24", "10.0.2.0/24"]
PRIVATE_SUBNET_CIDRS = ["10.0.11.0/24", "10.0.12.0/24"]

DB_NAME = "nexusdb"
DB_USER = "postgres"
DB_PASSWORD = "TrocarEssaSenha123!"  # NUNCA deixe hardcoded em produção — use Secrets Manager
DB_INSTANCE_CLASS = "db.t3.micro"

# No AWS Academy Learning Lab, EC2 e RDS só podem usar o LabInstanceProfile
# (ele já vem pré-criado no lab). Não é possível criar IAM roles novas.
EC2_INSTANCE_PROFILE = "LabInstanceProfile"
EC2_INSTANCE_TYPE = "t3.small"  # única classe liberada na maioria dos labs
# ATENÇÃO: t2.micro tem só 1GB RAM. O build do frontend (npm run build) pode
# estourar a memória. O script cria um swapfile pra mitigar isso, mas se o
# lab permitir, prefira t3.small.

# AMI Amazon Linux 2023 (us-east-1). Se sua região for outra, atualize.
EC2_AMI_ID = "ami-0453ec754f44f9a4a"

# Key pair usado para acessar as instâncias via SSH (crie antes no console, se precisar debugar)
EC2_KEY_NAME = None  # ex: "minha-chave" — deixe None se não for acessar via SSH

# Repositórios da aplicação (clonados direto na instância via user-data)
REPO_BACKEND_URL = "https://github.com/LeonardoJr96/backend_to_do_list.git"
REPO_FRONTEND_URL = "https://github.com/LeonardoJr96/frontend_to_do_list.git"

# Porta que o ALB e o Security Group liberam. Agora é 80 porque o Nginx,
# rodando no host, é quem recebe o tráfego (e faz proxy pro backend na 8000
# internamente, só em 127.0.0.1 — nunca exposto direto pra internet).
EC2_APP_PORT = 80

PROJECT_NAME = "nexus-todo"

# ============================================================
# CREDENCIAIS DO LEARNING LAB
# ============================================================
# O Learning Lab NÃO usa `aws configure` normal. Ele te dá 3 valores temporários
# na aba "AWS Details" -> "AWS CLI": Access Key, Secret Key e Session Token.
# Cole eles aqui (ou exporte como variáveis de ambiente) ANTES de rodar o script.
# Esses valores expiram em algumas horas — se o script falhar com erro de
# autenticação, é sinal de pegar credenciais novas no Learning Lab.

session = boto3.Session(
    aws_access_key_id="",
    aws_secret_access_key="",
    aws_session_token="",
    region_name=REGION,
)

ec2 = session.client("ec2")
elbv2 = session.client("elbv2")
rds = session.client("rds")
autoscaling = session.client("autoscaling")


def tag(resource_id, name):
    """Ajuda a nomear recursos — sem isso fica tudo com IDs feios no console."""
    ec2.create_tags(Resources=[resource_id], Tags=[{"Key": "Name", "Value": name}])


# ============================================================
# 1. VPC
# ============================================================
def create_vpc():
    print("Criando VPC...")
    vpc = ec2.create_vpc(CidrBlock=VPC_CIDR)["Vpc"]
    vpc_id = vpc["VpcId"]
    ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
    tag(vpc_id, f"{PROJECT_NAME}-vpc")

    # O RDS exige DNS habilitado na VPC para resolver o endpoint do banco
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    print(f"VPC criada: {vpc_id}")
    return vpc_id


# ============================================================
# 2. Availability Zones disponíveis na região
# ============================================================
def get_azs():
    azs = ec2.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}]
    )["AvailabilityZones"]
    return [az["ZoneName"] for az in azs[:2]]  # pega as 2 primeiras disponíveis


# ============================================================
# 3. Subnets — uma pública + uma privada por AZ
# ============================================================
def create_subnets(vpc_id, azs):
    public_subnets = []
    private_subnets = []

    for i, az in enumerate(azs):
        pub = ec2.create_subnet(
            VpcId=vpc_id, CidrBlock=PUBLIC_SUBNET_CIDRS[i], AvailabilityZone=az
        )["Subnet"]
        # Isso faz instâncias criadas nessa subnet ganharem IP público automaticamente
        ec2.modify_subnet_attribute(
            SubnetId=pub["SubnetId"], MapPublicIpOnLaunch={"Value": True}
        )
        tag(pub["SubnetId"], f"{PROJECT_NAME}-public-{az}")
        public_subnets.append(pub["SubnetId"])

        priv = ec2.create_subnet(
            VpcId=vpc_id, CidrBlock=PRIVATE_SUBNET_CIDRS[i], AvailabilityZone=az
        )["Subnet"]
        tag(priv["SubnetId"], f"{PROJECT_NAME}-private-{az}")
        private_subnets.append(priv["SubnetId"])

    print(f"Subnets públicas:  {public_subnets}")
    print(f"Subnets privadas:  {private_subnets}")
    return public_subnets, private_subnets


# ============================================================
# 4. Internet Gateway — porta de entrada/saída da VPC pra internet
# ============================================================
def create_igw(vpc_id):
    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    tag(igw_id, f"{PROJECT_NAME}-igw")
    print(f"Internet Gateway criado e anexado: {igw_id}")
    return igw_id


# ============================================================
# 5. NAT Gateway — vive na subnet pública, dá internet de SAÍDA às privadas
# ============================================================
def create_nat_gateway(public_subnet_id):
    eip = ec2.allocate_address(Domain="vpc")
    nat = ec2.create_nat_gateway(
        SubnetId=public_subnet_id, AllocationId=eip["AllocationId"]
    )["NatGateway"]
    nat_id = nat["NatGatewayId"]

    print("Aguardando NAT Gateway ficar disponível (leva alguns minutos)...")
    ec2.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat_id])
    tag(nat_id, f"{PROJECT_NAME}-nat")
    print(f"NAT Gateway pronto: {nat_id}")
    return nat_id


# ============================================================
# 6. Route Tables — definem para onde o tráfego de cada subnet vai
# ============================================================
def create_route_tables(vpc_id, igw_id, nat_id, public_subnets, private_subnets):
    # Rota pública: 0.0.0.0/0 -> Internet Gateway
    public_rt = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    ec2.create_route(
        RouteTableId=public_rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id
    )
    for subnet_id in public_subnets:
        ec2.associate_route_table(RouteTableId=public_rt, SubnetId=subnet_id)
    tag(public_rt, f"{PROJECT_NAME}-public-rt")

    # Rota privada: 0.0.0.0/0 -> NAT Gateway
    private_rt = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    ec2.create_route(
        RouteTableId=private_rt, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_id
    )
    for subnet_id in private_subnets:
        ec2.associate_route_table(RouteTableId=private_rt, SubnetId=subnet_id)
    tag(private_rt, f"{PROJECT_NAME}-private-rt")

    print("Route tables configuradas.")
    return public_rt, private_rt


# ============================================================
# 7. Security Groups — cadeia de confiança: Internet -> ALB -> App -> RDS
# ============================================================
def create_security_groups(vpc_id):
    # SG do Load Balancer: aceita HTTP/HTTPS de qualquer lugar
    alb_sg = ec2.create_security_group(
        GroupName=f"{PROJECT_NAME}-alb-sg",
        Description="Libera trafego web para o ALB",
        VpcId=vpc_id,
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=alb_sg,
        IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
             "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
             "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        ],
    )

    # SG da aplicação: só aceita tráfego que vem do ALB (não do mundo).
    # Porta 80 porque quem responde agora é o Nginx (proxy reverso pro
    # backend, que fica isolado em 127.0.0.1:8000 dentro da própria instância)
    app_sg = ec2.create_security_group(
        GroupName=f"{PROJECT_NAME}-app-sg",
        Description="Libera trafego apenas vindo do ALB",
        VpcId=vpc_id,
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=app_sg,
        IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": EC2_APP_PORT, "ToPort": EC2_APP_PORT,
             "UserIdGroupPairs": [{"GroupId": alb_sg}]},
        ],
    )

    # SG do RDS: só aceita conexão Postgres vinda da aplicação
    rds_sg = ec2.create_security_group(
        GroupName=f"{PROJECT_NAME}-rds-sg",
        Description="Libera Postgres apenas para a aplicacao",
        VpcId=vpc_id,
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=rds_sg,
        IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
             "UserIdGroupPairs": [{"GroupId": app_sg}]},
        ],
    )

    print(f"Security Groups -> ALB: {alb_sg} | App: {app_sg} | RDS: {rds_sg}")
    return alb_sg, app_sg, rds_sg


# ============================================================
# 8. Application Load Balancer
# ============================================================
def create_load_balancer(public_subnets, alb_sg, vpc_id):
    lb = elbv2.create_load_balancer(
        Name=f"{PROJECT_NAME}-alb",
        Subnets=public_subnets,
        SecurityGroups=[alb_sg],
        Scheme="internet-facing",
        Type="application",
        IpAddressType="ipv4",
    )["LoadBalancers"][0]
    lb_arn = lb["LoadBalancerArn"]

    # Target group: pra onde o ALB manda o tráfego. Porta 80 = Nginx.
    tg = elbv2.create_target_group(
        Name=f"{PROJECT_NAME}-tg",
        Protocol="HTTP",
        Port=EC2_APP_PORT,
        VpcId=vpc_id,
        TargetType="instance",
        HealthCheckPath="/health",
    )["TargetGroups"][0]
    tg_arn = tg["TargetGroupArn"]

    elbv2.create_listener(
        LoadBalancerArn=lb_arn,
        Protocol="HTTP",
        Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
    )

    print(f"Load Balancer criado: {lb['DNSName']}")
    return lb_arn, tg_arn


# ============================================================
# 9. Launch Template — "molde" que o Auto Scaling usa pra criar instâncias
#    (igual ao Lab 6, mas em vez de criar a AMI na mão, geramos o template
#    direto por código)
# ============================================================
import base64


def user_data_script(db_endpoint):
    """
    Script que roda automaticamente quando cada instância do Auto Scaling liga.

    O que ele faz, na ordem:
      1. Instala Docker, Git, Node.js 20 e Nginx.
      2. Cria um swapfile (t2.micro só tem 1GB RAM — o build do Vite pode
         estourar memória sem isso).
      3. Clona o backend (FastAPI) e sobe ele em um container Docker,
         escutando SÓ em 127.0.0.1:8000 (nunca exposto direto pra internet).
         A DATABASE_URL do RDS Postgres é injetada aqui — fica fixa no
         backend, o usuário final nunca vê nem digita esse dado.
      4. Clona o frontend (React/Vite), builda como arquivos estáticos
         (VITE_API_URL=/api é embutido no JS nessa hora).
      5. Configura o Nginx como porta de entrada única (porta 80): serve os
         arquivos do frontend e faz proxy reverso de /api/* pro backend.
         Isso resolve CORS de graça, porque front e API ficam na mesma
         origem do ponto de vista do navegador.
    """
    db_url = f"postgresql://{DB_USER}:{DB_PASSWORD}@{db_endpoint}:5432/{DB_NAME}"

    return f"""#!/bin/bash
set -e

# ---- Dependências base ----
dnf update -y
dnf install -y docker git nginx

# Node.js 20 (o AL2023 traz uma versão mais antiga por padrão via dnf)
curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -
dnf install -y nodejs

systemctl enable --now docker
usermod -aG docker ec2-user

# ---- Swap: evita OOM durante o npm run build em instância t2.micro (1GB RAM) ----
dd if=/dev/zero of=/swapfile bs=128M count=16
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile

# ---- Backend (FastAPI, rodando em container Docker) ----
cd /home/ec2-user
git clone {REPO_BACKEND_URL} backend
cd backend
docker build -t todo-backend .
docker run -d --name backend --restart always \\
  -p 127.0.0.1:8000:8000 \\
  -e DATABASE_URL="{db_url}" \\
  todo-backend

# ---- Frontend (React + Vite, build estático servido pelo Nginx) ----
cd /home/ec2-user
git clone {REPO_FRONTEND_URL} frontend
cd frontend
echo "VITE_API_URL=/api" > .env.production
npm ci
npm run build
mkdir -p /var/www/frontend
cp -r dist/* /var/www/frontend/

# ---- Nginx: serve o front e faz proxy reverso do /api pro backend ----
cat > /etc/nginx/conf.d/todo.conf << 'NGINXEOF'
server {{
    listen 80;

    root /var/www/frontend;
    index index.html;

    location /api/ {{
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}

    location /health {{
        return 200 'OK';
        add_header Content-Type text/plain;
    }}

    location / {{
        try_files $uri $uri/ /index.html;
    }}
}}
NGINXEOF

rm -f /etc/nginx/conf.d/default.conf
systemctl enable --now nginx
systemctl restart nginx
"""


def create_launch_template(app_sg, db_endpoint):
    lt_data = {
        "ImageId": EC2_AMI_ID,
        "InstanceType": EC2_INSTANCE_TYPE,
        "SecurityGroupIds": [app_sg],
        "IamInstanceProfile": {"Name": EC2_INSTANCE_PROFILE},
        "UserData": base64.b64encode(user_data_script(db_endpoint).encode()).decode(),
        "TagSpecifications": [{
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": f"{PROJECT_NAME}-web"}],
        }],
    }
    if EC2_KEY_NAME:
        lt_data["KeyName"] = EC2_KEY_NAME

    lt = ec2.create_launch_template(
        LaunchTemplateName=f"{PROJECT_NAME}-lt",
        LaunchTemplateData=lt_data,
    )["LaunchTemplate"]
    print(f"Launch Template criado: {lt['LaunchTemplateId']}")
    return lt["LaunchTemplateId"]


# ============================================================
# 10. Auto Scaling Group — mantém 2 a 6 instâncias atrás do ALB
#     (equivalente à Tarefa 3 do Lab 6, mas via código)
# ============================================================
def create_auto_scaling_group(lt_id, public_subnets, tg_arn):
    azs = list({
        s["AvailabilityZone"]
        for s in ec2.describe_subnets(SubnetIds=public_subnets)["Subnets"]
    })

    autoscaling.create_auto_scaling_group(
        AutoScalingGroupName=f"{PROJECT_NAME}-asg",
        LaunchTemplate={"LaunchTemplateId": lt_id, "Version": "$Latest"},
        MinSize=2,
        MaxSize=6,
        DesiredCapacity=2,
        VPCZoneIdentifier=",".join(public_subnets),
        AvailabilityZones=azs,
        TargetGroupARNs=[tg_arn],
        HealthCheckType="ELB",
        HealthCheckGracePeriod=120,
        Tags=[{
            "Key": "Name", "Value": f"{PROJECT_NAME}-web",
            "PropagateAtLaunch": True,
        }],
    )
    print("Auto Scaling Group criado (2 a 6 instâncias, atrás do ALB).")

    # Política de scaling por CPU — igual ao Lab 6 (alvo 60% de utilização)
    autoscaling.put_scaling_policy(
        AutoScalingGroupName=f"{PROJECT_NAME}-asg",
        PolicyName=f"{PROJECT_NAME}-scaling-policy",
        PolicyType="TargetTrackingScaling",
        TargetTrackingConfiguration={
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "ASGAverageCPUUtilization"
            },
            "TargetValue": 60.0,
        },
    )
    print("Política de Auto Scaling (CPU 60%) configurada.")


# ============================================================
# 11. RDS PostgreSQL — Multi-AZ, dentro das subnets privadas
# ============================================================
def create_rds(private_subnets, rds_sg):
    # DB Subnet Group precisa cobrir pelo menos 2 AZs — é isso que habilita o Multi-AZ
    rds.create_db_subnet_group(
        DBSubnetGroupName=f"{PROJECT_NAME}-db-subnet-group",
        DBSubnetGroupDescription="Subnets privadas para o RDS",
        SubnetIds=private_subnets,
    )

    print("Criando instância RDS Multi-AZ (leva de 5 a 10 minutos)...")
    rds.create_db_instance(
        DBInstanceIdentifier=f"{PROJECT_NAME}-db",
        DBName=DB_NAME,
        Engine="postgres",
        MasterUsername=DB_USER,
        MasterUserPassword=DB_PASSWORD,
        DBInstanceClass=DB_INSTANCE_CLASS,
        AllocatedStorage=20,
        VpcSecurityGroupIds=[rds_sg],
        DBSubnetGroupName=f"{PROJECT_NAME}-db-subnet-group",
        MultiAZ=True,             # cria réplica síncrona automática na 2ª AZ
        PubliclyAccessible=False,  # fica só acessível de dentro da VPC
        BackupRetentionPeriod=7,
        StorageEncrypted=True,
    )

    rds.get_waiter("db_instance_available").wait(
        DBInstanceIdentifier=f"{PROJECT_NAME}-db"
    )

    endpoint = rds.describe_db_instances(
        DBInstanceIdentifier=f"{PROJECT_NAME}-db"
    )["DBInstances"][0]["Endpoint"]["Address"]
    print(f"RDS disponível em: {endpoint}")
    return endpoint


# ============================================================
# EXECUÇÃO — a ordem aqui importa (cada recurso depende do anterior)
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

    # RDS demora 5-10min pra ficar pronto — criamos antes do Auto Scaling pra já
    # nascer sabendo o endpoint do banco (fica fixo no backend do app)
    db_endpoint = create_rds(private_subnets, rds_sg)

    lt_id = create_launch_template(app_sg, db_endpoint)
    create_auto_scaling_group(lt_id, public_subnets, tg_arn)

    lb_dns = elbv2.describe_load_balancers(LoadBalancerArns=[lb_arn])[
        "LoadBalancers"
    ][0]["DNSName"]

    print("\n=== INFRAESTRUTURA CRIADA COM SUCESSO ===")
    print(f"VPC ID:        {vpc_id}")
    print(f"DB Endpoint:   {db_endpoint}")
    print(f"URL do App:    http://{lb_dns}")
    print("\nO Auto Scaling Group vai demorar 1-2min pra subir as 2 instâncias iniciais.")
    print("Aguarde mais ~1min pro health check do ALB ficar 'healthy' antes de testar a URL.")