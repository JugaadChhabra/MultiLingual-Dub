import boto3
import os
from botocore.exceptions import ClientError

wasabi_endpoint = "https://s3.wasabisys.com"


s3_client = boto3.client(
    "s3",
    endpoint_url=wasabi_endpoint,
    aws_access_key_id="DNZD4UYK5TR7LXH21O9S",
    aws_secret_access_key="OeAPjZdkS7KikH5lPzfjXZAcXOFH7lJa18HDHtvh",
)

def upload_file_to_wasabi(bucket_name, file_path, subdirectory="uploads"):
    """
    Uploads a file to Wasabi, creating the bucket and/or subdirectory if they don't exist.

    :param bucket_name: Name of the Wasabi bucket
    :param file_path: Local path to the file you want to upload
    :param subdirectory: Subdirectory (prefix) inside the bucket — defaults to 'uploads'
    """

    # --- 1. Ensure bucket exists ---
    try:
        s3_client.head_bucket(Bucket=bucket_name)
        print(f"Bucket '{bucket_name}' already exists.")
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("404", "NoSuchBucket"):
            print(f"Bucket '{bucket_name}' not found. Creating...")
            s3_client.create_bucket(Bucket=bucket_name)
            print(f"Bucket '{bucket_name}' created.")
        else:
            raise

    # --- 2. Ensure subdirectory exists (in S3/Wasabi, directories are just key prefixes) ---
    subdir_key = f"{subdirectory}/"  # S3 "folders" are just keys ending in /
    try:
        s3_client.head_object(Bucket=bucket_name, Key=subdir_key)
        print(f"Subdirectory '{subdirectory}/' already exists.")
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("404", "NoSuchKey"):
            print(f"Subdirectory '{subdirectory}/' not found. Creating...")
            s3_client.put_object(Bucket=bucket_name, Key=subdir_key)
            print(f"Subdirectory '{subdirectory}/' created.")
        else:
            raise

    # --- 3. Upload the file into the subdirectory ---
    file_name = os.path.basename(file_path)
    destination_key = f"{subdirectory}/{file_name}"

    print(f"Uploading '{file_name}' to '{bucket_name}/{destination_key}'...")
    s3_client.upload_file(file_path, bucket_name, destination_key)
    print(f"Upload complete: s3://{bucket_name}/{destination_key}")


# --- Usage ---
upload_file_to_wasabi(
    bucket_name="testi",
    file_path="/Users/jugaadchhabra/Documents/Github/MutiLingual-Dub/requirements.txt",
    subdirectory="testing"
)