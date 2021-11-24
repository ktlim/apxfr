#!/usr/bin/env python

from __future__ import annotations
import abc
import argparse
from datetime import datetime, timedelta
import functools
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
from urllib3.connection import HTTPConnection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transfer a stream of CCD images."
    )
    parser.add_argument('-d', '--destination', metavar='URL', required=True,
                        help=("URL with"
                              " gsapi, boto, minio, https, http, bbcp, scp"
                              " scheme"))
    parser.add_argument('-s', '--starttime', metavar='HH:MM', required=True,
                        help="local time to start simulation")
    parser.add_argument('-n', '--numexp', metavar='EXPOSURES', type=int,
                        required=True, help="number of exposures to simulate")
    parser.add_argument('-c', '--ccds', metavar='CCDS', type=int,
                        default=1, help="number of CCDs to simulate")
    parser.add_argument('-i', '--interval', type=int, default=17,
                        help="interval between exposures in sec")
    parser.add_argument('-I', '--inputfile', type=Path,
                        default="./data/S00.fits", help="input directory")
    parser.add_argument('-t', '--tempdir', type=Path, default="/tmp",
                        help="temporary directory")
    parser.add_argument('-z', '--compress', action='store_true',
                        help="compress before transfer")
    parser.add_argument('-P', '--private', action='store_true',
                        help="use private Google Cloud interconnect")
    parser.add_argument('-K', '--keepalive', action='store_true',
                        help="use TCP keepalive options")
    return parser


class Waiter:
    def __init__(self, hour: int, minute: int, interval: int):
        self.base_time = datetime.now().replace(hour=hour, minute=minute,
                                                second=0, microsecond=0)
        self.interval = interval

    def wait_exposure(self, num: int):
        when = self.base_time + timedelta(seconds=num * self.interval)
        delay = (when - datetime.now()).total_seconds()
        delay_str = f"{abs(delay)} seconds for exposure {num} at {when}"
        if delay < 0:
            logging.info("Late " + delay_str)
            return
        logging.info("Sleeping " + delay_str)
        time.sleep(delay)


def log_timing(func):

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        logging.info(f"Start {func.__name__}")
        start = time.time()
        try:
            res = func(self, *args, **kwargs)
        finally:
            delta = time.time() - start
            logging.info(f"End {func.__name__} = {delta}")
        return res

    return wrapper


@log_timing
def copy(source: Path, temp: Path, dest: Path, compress: bool = False) -> Path:
    (temp / dest).parent.mkdir(parents=True, exist_ok=True)
    logging.info(f"Copying {source} to {temp / dest}")
    subprocess.run(["cp", f"{source}", f"{temp / dest}"])
    if compress:
        logging.info(f"Compressing {temp / dest}")
        subprocess.run(["fpack", f"{temp / dest}"])
        dest = dest.with_suffix(".fits.fz")
    return dest


class Uploader(abc.ABC):
    @classmethod
    def create(cls, dest: str) -> Uploader:
        logging.info(f"Creating uploader for {dest}")
        if dest.startswith("gsapi://"):
            return GsapiUploader(dest[len("gsapi://"):])
        if dest.startswith("boto://"):
            return GsapiUploader(dest[len("boto://"):])
        if dest.startswith("minio://"):
            return GsapiUploader(dest[len("minio://"):])
        if dest.startswith("https://") or dest.startswith("http://"):
            return HttpUploader(dest)
        if dest.startswith("bbcp://"):
            return HttpUploader(dest[len("bbcp://"):])
        if dest.startswith("scp://"):
            return ScpUploader(dest[len("scp://"):])
        raise RuntimeError(f"Unrecognized URL {dest}")

    def transfer(self, temp_dir: Path, source: Path):
        raise NotImplementedError("transfer not implemented")


class GsapiUploader(Uploader):
    def __init__(self, dest: str):
        from google.cloud import storage
        if "/" in dest:
            bucket, self.prefix = dest.split("/", 1)
        else:
            bucket = dest
            self.prefix = ""
        logging.info(f"gsapi: opening bucket {bucket}"
                     f", saving prefix '{self.prefix}'")
        self.bucket = storage.Client().bucket(bucket)
        try:
            _ = self.bucket.blob(".null").download_as_string()
        except Exception as exc:
            logging.info(f"Ignored: {exc}")

    @log_timing
    def transfer(self, temp_dir: Path, source: Path):
        logging.info(f"gsapi: uploading to {self.prefix}/{source}")
        size = 100*1024*1024
        if self.prefix == "":
            blob = self.bucket.blob(f"{source}", chunk_size=size)
        else:
            blob = self.bucket.blob(f"{self.prefix}/{source}", chunk_size=size)
        blob.upload_from_filename(temp_dir / source)


class BotoUploader(Uploader):
    def __init__(self, dest: str):
        import boto3
        host, self.bucket, self.prefix = dest.split("/", 2)
        logging.info(f"boto: opening host {host}, saving bucket {self.bucket}"
                     f", prefix '{self.prefix}'")
        self.client = boto3.client('s3')
        self.client.put_object(Bucket=self.bucket, Key=".null",
                               Body=b"", ContentLength=0)

    @log_timing
    def transfer(self, temp_dir: Path, source: Path):
        logging.info(f"boto: uploading to {self.prefix}/{source}")
        self.client.upload_file(temp_dir / source,
                                self.bucket, f"{self.prefix}/{source}")


class MinioUploader(Uploader):
    def __init__(self, dest: str):
        from io import BytesIO
        from minio import Minio
        host, self.bucket, self.prefix = dest.split("/", 2)
        logging.info(f"minio: opening host {host}, saving bucket {self.bucket}"
                     f", prefix '{self.prefix}'")
        self.conn = Minio(host)
        self.conn.put_object(self.bucket, ".null", BytesIO(b""), 0)

    @log_timing
    def transfer(self, temp_dir: Path, source: Path):
        logging.info(f"minio: uploading to {self.prefix}/{source}")
        self.conn.fput_object(
            self.bucket,
            f"{self.prefix}/{source}",
            temp_dir / source
        )


class HttpUploader(Uploader):
    def __init__(self, dest: str):
        import requests
        logging.info(f"http: opening session to {dest}")
        self.url = dest
        self.session = requests.Session()

    @log_timing
    def transfer(self, temp_dir: Path, source: Path):
        logging.info(f"http: putting to {self.url}/{source}")
        with (temp_dir / source).open("rb") as s:
            r = self.session.put(f"{self.url}/{source}", data=s)
        r.raise_for_status()


class BbcpUploader(Uploader):
    def __init__(self, dest: str):
        self.host, path = dest.split("/", 1)
        logging.info(f"bbcp: saving host {self.host} and path {path}")
        self.path = Path(path)

    @log_timing
    def transfer(self, temp_dir: Path, source: Path):
        logging.info(f"bbcp: dir {self.path / source.parent}; file {source}")
        subprocess.run(["bbcp", "-A", self.path / source,
                        f"{self.host}:{self.path / source}"])


class ScpUploader(Uploader):
    def __init__(self, dest: str):
        self.host, path = dest.split("/", 1)
        logging.info(f"scp: saving host {self.host} and path {path}")
        self.path = Path(path)

    @log_timing
    def transfer(self, temp_dir: Path, source: Path):
        logging.info(f"scp: dir {self.path / source.parent}; file {source}")
        with (temp_dir / source).open("rb") as s:
            subprocess.run(["ssh", self.host,
                            f"mkdir -p {self.path / source.parent};"
                            f"cat > {self.path / source}"],
                           stdin=s)


def simulate(
    ccd_name: str,
    starttime: str,
    destination: str,
    interval: int,
    tempdir: Path,
    numexp: int,
    inputfile: str,
    compress: bool,
) -> None:
    logging.basicConfig(
        format=f"{ccd_name}" + " {asctime} {message}",
        style="{",
        level="INFO"
    )

    print(f"Socket opts = {HTTPConnection.default_socket_options}")

    hour, minute = starttime.split(":")
    seqnum_start = int(hour + minute) * 10

    uploader = Uploader.create(destination)

    waiter = Waiter(int(hour), int(minute), interval)

    now = datetime.now()
    obs_day = now.strftime("%Y%m%d")
    obs_day_str = now.strftime("%Y-%m-%d")

    with tempfile.TemporaryDirectory(dir=tempdir) as temp_dir:
        logging.info(f"Using temp directory {temp_dir}")
        temp_path = Path(temp_dir)
        for i in range(numexp):
            waiter.wait_exposure(i)
            seqnum = seqnum_start + i
            source_path = inputfile

            dest_path = Path(obs_day_str).joinpath(
                f"{obs_day}{seqnum:05d}",
                f"MC_O_{obs_day}_{seqnum:05d}_{ccd_name}.fits"
            )
            logging.info(f"Copying from {source_path} to"
                         f" {temp_path / dest_path}"
                         f" with compress = {compress}")
            dest_path = copy(source_path, temp_path, dest_path, compress)
            uploader.transfer(temp_path, dest_path)


def main():
    if "APXFR_KEY" in os.environ:
        print("Using APXFR_KEY credentials")
        with open("/root/secret.json", "w") as f:
            print(os.environ["APXFR_KEY"], file=f)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/root/secret.json"

    parser = build_parser()
    args = parser.parse_args()

    host_name = socket.gethostname()
    node_match = re.search(r'-(\d+)$', host_name)
    if node_match:
        node_num = int(node_match[1])
    else:
        node_match = re.search(r'-(\w{5})$', host_name)
        if node_match:
            node_num = int(node_match[1], 36)
        else:
            node_match = re.search(r'(\d+)', host_name)
            if node_match:
                node_num = int(node_match[1])
            else:
                node_num = 0
    if args.private:
        print("Using private network")
        with open("/etc/hosts", "a") as f:
            print(f"199.36.153.{int(node_num) % 4 + 8} storage.googleapis.com",
                  file=f)
    if args.keepalive:
        print("Using TCP keepalive")
        HTTPConnection.default_socket_options += [
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
            (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 1),
            (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 1)
        ]

    jobs = []
    for ccd in range(args.ccds):
        pid = os.fork()
        if pid == 0:
            simulate(
                f"{node_num}-{ccd}",
                args.starttime,
                args.destination,
                args.interval,
                args.tempdir,
                args.numexp,
                args.inputfile,
                args.compress
            )
            logging.info("Child process exiting")
            exit(0)
        else:
            jobs.append(pid)
    for job in jobs:
        os.waitpid(job, 0)

    print("Main process sleeping")
    while True:
        time.sleep(1000000)
        pass


if __name__ == "__main__":
    main()
