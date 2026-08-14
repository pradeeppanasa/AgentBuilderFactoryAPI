"""Exit 0 iff DynamoDB Local is accepting requests — used by
scripts/local-setup.sh's wait-for-healthy loop. Not a general-purpose
script; kept tiny and dependency-free (just boto3) on purpose.
"""

import boto3

boto3.client(
    "dynamodb",
    region_name="eu-west-2",
    endpoint_url="http://localhost:8001",
    aws_access_key_id="local",
    aws_secret_access_key="local",
).list_tables()
