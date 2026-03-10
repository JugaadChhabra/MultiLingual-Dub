import boto3
import os
from botocore.exceptions import ClientError

region="s3.ap-southeast-1.wasabisys.com"

wasabi_endpoint = "https://s3.ap-southeast-1.wasabisys.com"


s3_client = boto3.client(
    "s3",
    endpoint_url=wasabi_endpoint,
    aws_access_key_id="",
    aws_secret_access_key="",
    region_name="ap-southeast-1",
)

def upload_file(bucket_name, file_path, folder_name):
    try:
        s3_client.head_object(Bucket=bucket_name, Key=f"{folder_name}/")
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            s3_client.put_object(Bucket=bucket_name, Key=f"{folder_name}/")
            print(f"Folder '{folder_name}' created.")

    file_name = os.path.basename(file_path)
    s3_client.upload_file(file_path, bucket_name, f"{folder_name}/{file_name}")
    print(f"Uploaded: {file_name} → {bucket_name}/{folder_name}/{file_name}")

upload_file("playschoolaudio", "/Users/jugaadchhabra/Documents/Github/MutiLingual-Dub/requirements.txt", "testi-folder")