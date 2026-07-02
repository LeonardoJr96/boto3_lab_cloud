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
    answer = input(f"{message} [sim/nao]: ").strip().lower()
    return answer in {"s", "sim", "y", "yes"}


def wait_for(condition: Callable[[], bool], description: str, timeout: int = 600, interval: int = 10) -> None:
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
        if code in {
            "ResourceNotFoundException",
            "InvalidParameterValue",
            "DependencyViolation",
            "InvalidLoadBalancerNameException",
            "InvalidLaunchTemplateName.NotFoundException",
            "InvalidLaunchTemplateId.NotFoundException",
            "LoadBalancerNotFoundException",
            "LoadBalancerNotFound",
            "TargetGroupNotFoundException",
            "TargetGroupNotFound",
        }:
            return
        raise


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
        safe_delete(elbv2.delete_load_balancer, LoadBalancerArn=lb["LoadBalancerArn"])

    try:
        target_groups = elbv2.describe_target_groups(Names=[f"{PROJECT_NAME}-tg"])["TargetGroups"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"TargetGroupNotFoundException", "TargetGroupNotFound"}:
            target_groups = []
        else:
            raise

    for target_group in target_groups:
        safe_delete(elbv2.delete_target_group, TargetGroupArn=target_group["TargetGroupArn"])


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
            return not instances or instances[0]["DBInstanceStatus"] == "deleting"
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
    print("Removendo security groups...")
    response = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    for sg in response.get("SecurityGroups", []):
        name = sg.get("GroupName", "")
        if name in {f"{PROJECT_NAME}-alb-sg", f"{PROJECT_NAME}-app-sg", f"{PROJECT_NAME}-rds-sg"}:
            safe_delete(ec2.delete_security_group, GroupId=sg["GroupId"])


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
