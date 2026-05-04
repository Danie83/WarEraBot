import os
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

def ensure_tables(users_table: str = "users", diplomacies_table: str = "diplomacies", region: Optional[str] = None) -> bool:
    """Ensure both users and diplomacies tables exist in DynamoDB.

    Returns True if at least one table was confirmed/created. If AWS creds
    are missing, returns False.
    """
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    session_token = os.environ.get("AWS_SESSION_TOKEN")
    region = region or os.environ.get("AWS_REGION") or "eu-west-1"

    if not access_key or not secret_key:
        return False

    client = boto3.client(
        "dynamodb",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
    )

    created_any = False

    # Users table (key: api_id)
    try:
        client.describe_table(TableName=users_table)
        created_any = True
    except ClientError as e:
        err = e.response.get("Error", {})
        if err.get("Code") != "ResourceNotFoundException":
            raise
        try:
            client.create_table(
                TableName=users_table,
                AttributeDefinitions=[
                    {"AttributeName": "api_id", "AttributeType": "S"},
                    {"AttributeName": "discord_username", "AttributeType": "S"},
                    {"AttributeName": "display_name", "AttributeType": "S"},
                ],
                KeySchema=[
                    {
                        "AttributeName": "api_id",
                        "KeyType": "HASH",  # primary key
                    }
                ],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "discord_username-index",
                        "KeySchema": [
                            {
                                "AttributeName": "discord_username",
                                "KeyType": "HASH",
                            }
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                    {
                        "IndexName": "display_name-index",
                        "KeySchema": [
                            {
                                "AttributeName": "display_name",
                                "KeyType": "HASH",
                            }
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            waiter = client.get_waiter("table_exists")
            waiter.wait(TableName=users_table, WaiterConfig={"Delay": 2, "MaxAttempts": 25})
            created_any = True
        except ClientError as ce:
            if ce.response.get("Error", {}).get("Code") == "ResourceInUseException":
                created_any = True
            else:
                raise

    # Diplomacies table (key: country_name)
    try:
        client.describe_table(TableName=diplomacies_table)
        created_any = True
    except ClientError as e:
        err = e.response.get("Error", {})
        if err.get("Code") != "ResourceNotFoundException":
            raise
        try:
            client.create_table(
                TableName=diplomacies_table,
                AttributeDefinitions=[
                    {"AttributeName": "country_name", "AttributeType": "S"},
                    {"AttributeName": "status", "AttributeType": "S"},
                ],
                KeySchema=[
                    {
                        "AttributeName": "country_name",
                        "KeyType": "HASH",
                    }
                ],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "status-index",
                        "KeySchema": [
                            {
                                "AttributeName": "status",
                                "KeyType": "HASH",
                            }
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            waiter = client.get_waiter("table_exists")
            waiter.wait(TableName=diplomacies_table, WaiterConfig={"Delay": 2, "MaxAttempts": 25})
            created_any = True
        except ClientError as ce:
            if ce.response.get("Error", {}).get("Code") == "ResourceInUseException":
                created_any = True
            else:
                raise

    return created_any
