#!/usr/bin/env python3
"""Reinicia a infraestrutura criada pelo script main.py da pasta cloud.

Uso:
  python restart.py               # destrói e recria a infraestrutura
  python restart.py --destroy-only  # apenas destrói a infraestrutura
  python restart.py --yes          # pula a confirmação interativa
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import main as infra_main  # noqa: E402

PROJECT_NAME = infra_main.PROJECT_NAME
ec2 = infra_main.ec2
elbv2 = infra_main.elbv2
rds = infra_main.rds
autoscaling = infra_main.autoscaling


def confirm(message: str, skip: bool) -> bool:
    if skip:
        return True
    #answer = input(f"{message} [sim/nao]: ").strip().lower()
    answer = "sim"
    return answer in {"s", "sim", "y", "yes"}


def wait_for(condition: Callable[[], bool], description: str, timeout: int = 1000, interval: int = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise TimeoutError(f"Tempo esgotado aguardando: {description}")


def safe_delete(fn: Callable, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        # NOTA: "DependencyViolation" foi removido de propósito desta lista.
        # Antes ele era engolido aqui, o que fazia o script reportar sucesso
        # mesmo com VPC/subnet/security group ainda existindo na AWS (ainda
        # referenciados por outro recurso). Agora ele estoura de verdade,
        # pra você ver o erro real em vez de descobrir só na próxima recriação.
        if code in {
            "ResourceNotFoundException",
            "InvalidParameterValue",
            "InvalidLoadBalancerNameException",
            "InvalidLaunchTemplateName.NotFoundException",
            "InvalidLaunchTemplateId.NotFoundException",
            "LoadBalancerNotFoundException",
            "LoadBalancerNotFound",
            "TargetGroupNotFoundException",
            "TargetGroupNotFound",
            "DBInstanceNotFound",
            "DBInstanceNotFoundFault",
            "NatGatewayNotFound",
        }:
            return
        raise


def wait_for_instances_terminated(timeout: int = 1000, interval: int = 10) -> None:
    def none_running() -> bool:
        response = ec2.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [f"{PROJECT_NAME}-web"]},
                {
                    "Name": "instance-state-name",
                    # "shutting-down" foi adicionado: é o estado transitório
                    # entre terminate_instances() e "terminated". Sem ele,
                    # a checagem achava (erroneamente) que nenhuma instância
                    # estava rodando enquanto a ENI ainda estava anexada,
                    # liberando o script pra apagar subnet/security group
                    # cedo demais e provocar DependencyViolation.
                    "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
                },
            ]
        )
        instance_ids = [
            instance["InstanceId"]
            for reservation in response.get("Reservations", [])
            for instance in reservation.get("Instances", [])
        ]
        return not instance_ids

    wait_for(none_running, "instâncias EC2 terminarem", timeout=timeout, interval=interval)


def wait_for_load_balancer_deleted(lb_arn: str, timeout: int = 1000, interval: int = 15) -> None:
    def is_deleted() -> bool:
        try:
            lbs = elbv2.describe_load_balancers(LoadBalancerArns=[lb_arn])["LoadBalancers"]
            return not lbs
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            return code in {"LoadBalancerNotFoundException", "LoadBalancerNotFound"}

    wait_for(is_deleted, "Load Balancer ser removido", timeout=timeout, interval=interval)


def wait_for_target_group_deleted(tg_arn: str, timeout: int = 1000, interval: int = 15) -> None:
    def is_deleted() -> bool:
        try:
            tgs = elbv2.describe_target_groups(TargetGroupArns=[tg_arn])["TargetGroups"]
            return not tgs
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            return code in {"TargetGroupNotFoundException", "TargetGroupNotFound"}

    wait_for(is_deleted, "Target Group ser removido", timeout=timeout, interval=interval)


def find_vpc_id() -> str | None:
    response = ec2.describe_vpcs(
        Filters=[{"Name": "tag:Name", "Values": [f"{PROJECT_NAME}-vpc"]}]
    )
    vpcs = response.get("Vpcs", [])
    return vpcs[0]["VpcId"] if vpcs else None


def delete_autoscaling_group() -> None:
    print("Removendo Auto Scaling Group...")
    try:
        autoscaling.delete_auto_scaling_group(
            AutoScalingGroupName=f"{PROJECT_NAME}-asg",
            ForceDelete=True,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ValidationError":
            raise

    def is_gone() -> bool:
        try:
            groups = autoscaling.describe_auto_scaling_groups(
                AutoScalingGroupNames=[f"{PROJECT_NAME}-asg"]
            )["AutoScalingGroups"]
            return not groups
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ValidationError":
                return True
            raise

    wait_for(is_gone, "ASG ser removido", timeout=300)


def delete_launch_template() -> None:
    print("Removendo Launch Template...")
    safe_delete(
        ec2.delete_launch_template,
        LaunchTemplateName=f"{PROJECT_NAME}-lt",
    )


def delete_infra_instances() -> None:
    print("Encerrando instâncias EC2 remanescentes...")
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [f"{PROJECT_NAME}-web"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ]
    )

    instance_ids = [
        instance["InstanceId"]
        for reservation in response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]

    if instance_ids:
        ec2.terminate_instances(InstanceIds=instance_ids)
        wait_for_instances_terminated(timeout=1000)


def delete_load_balancer() -> None:
    print("Removendo Load Balancer e Target Group...")
    try:
        lbs = elbv2.describe_load_balancers(Names=[f"{PROJECT_NAME}-alb"])["LoadBalancers"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"LoadBalancerNotFoundException", "LoadBalancerNotFound"}:
            lbs = []
        else:
            raise

    for lb in lbs:
        lb_arn = lb["LoadBalancerArn"]
        safe_delete(elbv2.delete_load_balancer, LoadBalancerArn=lb_arn)
        wait_for_load_balancer_deleted(lb_arn)

    try:
        target_groups = elbv2.describe_target_groups(Names=[f"{PROJECT_NAME}-tg"])["TargetGroups"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"TargetGroupNotFoundException", "TargetGroupNotFound"}:
            target_groups = []
        else:
            raise

    for target_group in target_groups:
        tg_arn = target_group["TargetGroupArn"]
        safe_delete(elbv2.delete_target_group, TargetGroupArn=tg_arn)
        wait_for_target_group_deleted(tg_arn)


def delete_rds() -> None:
    print("Removendo instância RDS...")
    try:
        rds.delete_db_instance(
            DBInstanceIdentifier=f"{PROJECT_NAME}-db",
            SkipFinalSnapshot=True,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in {"DBInstanceNotFound", "InvalidDBInstanceStateFault"}:
            raise

    def is_deleted() -> bool:
        try:
            instances = rds.describe_db_instances(DBInstanceIdentifier=f"{PROJECT_NAME}-db")["DBInstances"]
            # Antes: considerava "pronto" assim que o status virava "deleting",
            # mas nesse momento a instância ainda existe fisicamente e ainda
            # está usando a DB Subnet Group -- o delete_db_subnet_group logo
            # abaixo podia falhar com InvalidDBSubnetGroupStateFault (erro não
            # tratado). Agora espera a instância sumir de verdade da listagem.
            return not instances
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"DBInstanceNotFound", "DBInstanceNotFoundFault"}:
                return True
            raise

    wait_for(is_deleted, "RDS ser removido", timeout=900)

    safe_delete(
        rds.delete_db_subnet_group,
        DBSubnetGroupName=f"{PROJECT_NAME}-db-subnet-group",
    )


def delete_nat_gateway_and_eip() -> None:
    print("Removendo NAT Gateway e EIP...")
    try:
        gateways = ec2.describe_nat_gateways(
            Filter=[{"Name": "tag:Name", "Values": [f"{PROJECT_NAME}-nat"]}]
        )["NatGateways"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NatGatewayNotFound":
            gateways = []
        else:
            raise

    for gateway in gateways:
        nat_id = gateway["NatGatewayId"]
        safe_delete(ec2.delete_nat_gateway, NatGatewayId=nat_id)

        def nat_gone() -> bool:
            try:
                gws = ec2.describe_nat_gateways(NatGatewayIds=[nat_id])["NatGateways"]
                return not gws or all(g["State"] in {"deleted", "deleting"} for g in gws)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                return code == "NatGatewayNotFound"

        wait_for(nat_gone, "NAT Gateway ser removido", timeout=1000, interval=15)

        for address in gateway.get("NatGatewayAddresses", []):
            allocation_id = address.get("AllocationId")
            if allocation_id:
                safe_delete(ec2.release_address, AllocationId=allocation_id)


def delete_route_tables(vpc_id: str) -> None:
    print("Removendo route tables...")
    route_tables = ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )["RouteTables"]

    for rt in route_tables:
        name = next((tag["Value"] for tag in rt.get("Tags", []) if tag["Key"] == "Name"), "")
        if name not in {f"{PROJECT_NAME}-public-rt", f"{PROJECT_NAME}-private-rt"}:
            continue

        for association in rt.get("Associations", []):
            if not association.get("Main", False):
                ec2.disassociate_route_table(AssociationId=association["RouteTableAssociationId"])

        safe_delete(ec2.delete_route_table, RouteTableId=rt["RouteTableId"])


def delete_subnets(vpc_id: str) -> None:
    print("Removendo subnets...")
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    for subnet in subnets:
        safe_delete(ec2.delete_subnet, SubnetId=subnet["SubnetId"])


def delete_security_groups(vpc_id: str) -> None:
    # Reescrito para apagar em múltiplas passagens: alb-sg / app-sg / rds-sg
    # costumam se referenciar entre si (ex: rds-sg libera ingress vindo de
    # app-sg, que libera ingress vindo de alb-sg). A AWS não deixa apagar um
    # security group enquanto outro ainda o referencia, então uma única
    # passagem na ordem "errada" sempre falhava com DependencyViolation --
    # erro que antes era engolido pelo safe_delete e mascarava o problema.
    print("Removendo security groups...")
    target_names = {f"{PROJECT_NAME}-alb-sg", f"{PROJECT_NAME}-app-sg", f"{PROJECT_NAME}-rds-sg"}

    for _ in range(5):  # tentativas suficientes pra resolver a ordem de dependência
        response = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
        remaining = [sg for sg in response.get("SecurityGroups", []) if sg.get("GroupName") in target_names]
        if not remaining:
            return

        deleted_any = False
        for sg in remaining:
            try:
                ec2.delete_security_group(GroupId=sg["GroupId"])
                deleted_any = True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "DependencyViolation":
                    raise
                # ainda referenciado por outro SG do grupo; tenta de novo na próxima passada

        if not deleted_any:
            time.sleep(5)  # nenhum progresso nessa passada, espera um pouco e tenta de novo

    raise RuntimeError(
        "Não foi possível remover todos os security groups "
        f"({PROJECT_NAME}-alb-sg / {PROJECT_NAME}-app-sg / {PROJECT_NAME}-rds-sg) "
        "após 5 tentativas -- verifique dependências manualmente no console."
    )


def delete_internet_gateway(vpc_id: str) -> None:
    print("Removendo Internet Gateway...")
    response = ec2.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
    )
    for igw in response.get("InternetGateways", []):
        safe_delete(ec2.detach_internet_gateway, InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
        safe_delete(ec2.delete_internet_gateway, InternetGatewayId=igw["InternetGatewayId"])


def delete_vpc(vpc_id: str) -> None:
    print("Removendo VPC...")
    safe_delete(ec2.delete_vpc, VpcId=vpc_id)


def destroy_infra() -> None:
    print("Iniciando destruição da infraestrutura da AWS...")
    delete_autoscaling_group()
    delete_launch_template()
    delete_load_balancer()
    delete_infra_instances()
    delete_rds()
    delete_nat_gateway_and_eip()

    vpc_id = find_vpc_id()
    if not vpc_id:
        print("Nenhuma VPC encontrada para remover; encerrando.")
        return

    delete_route_tables(vpc_id)
    delete_subnets(vpc_id)
    delete_security_groups(vpc_id)
    delete_internet_gateway(vpc_id)
    delete_vpc(vpc_id)
    print("Infraestrutura removida com sucesso.")


def recreate_infra() -> None:
    print("Recriando a infraestrutura...")
    subprocess.run([sys.executable, str(ROOT / "main.py")], cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Destrói e/ou recria a infraestrutura da pasta cloud")
    parser.add_argument("--destroy-only", action="store_true", help="apenas remove os recursos AWS")
    parser.add_argument("--yes", action="store_true", help="pula a confirmação interativa")
    args = parser.parse_args()

    if not confirm("Isso vai remover recursos da AWS. Deseja continuar?", args.yes):
        print("Operação cancelada.")
        return

    destroy_infra()
    if not args.destroy_only:
        recreate_infra()


if __name__ == "__main__":
    main()