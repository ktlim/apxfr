#!/bin/env python

from __future__ import annotations
import abc
import argparse
from datetime import datetime, timedelta
import functools
import logging
from pathlib import Path
import subprocess
import tempfile
import time
from typing import List


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transfer a stream of CCD images."
    )
    parser.add_argument('ccd_list', metavar='CCD', nargs='+',
                        help=("CCD names are in RNN-SNN form"
                              ", R00-S00 for LATISS"))
    parser.add_argument('-d', '--destination', metavar='URL', required=True,
                        help=("destination bucket"
                              " or scp host:path"
                              " or HTTPS PUT URL"))
    parser.add_argument('-s', '--starttime', metavar='HH:MM', required=True,
                        help="local time to start simulation")
    parser.add_argument('-n', '--numexp', metavar='EXPOSURES', type=int,
                        required=True,
                        help="number of exposures to simulate")
    parser.add_argument('-c', '--camera', default="AT",
                        choices=("AT", "CC", "MC"),
                        help="abbreviation for camera to simulate")
    parser.add_argument('-i', '--interval', type=int, default=17,
                        help="interval between exposures in sec")
    parser.add_argument('-I', '--inputdir', default=".", type=Path,
                        help="input directory")
    parser.add_argument('-t', '--tempdir', default="/tmp", type=Path,
                        help="temporary directory")
    parser.add_argument('-z', '--compress', action='store_true',
                        help="compress before transfer")
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


def find_input(dir: Path, ccd: str) -> Path:
    # Look for full focal plane
    input = dir / f"{ccd}.fits"
    if input.exists():
        return input

    # Look for single raft
    if "-" in ccd:
        raft, sensor = ccd.split("-", 1)
        input = dir / f"{sensor}.fits"
        if input.exists():
            return input

    # Look for single sensor
    input = dir / "S00.fits"
    if input.exists():
        return input

    raise RuntimeError(f"No CCD file found for {ccd}")


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
    if compress:
        dest = dest.with_suffix(".fits.gz")
        logging.info(f"Copying {source} to {temp / dest}")
        with source.open("rb") as s:
            with (temp / dest).open("wb") as d:
                subprocess.run(["gzip"], stdin=s, stdout=d)
    else:
        logging.info(f"Copying {source} to {temp / dest}")
        subprocess.run(["cp", f"{source}", f"{temp / dest}"])
    return dest


class Uploader(abc.ABC):
    @classmethod
    def create(cls, dest: str) -> Uploader:
        logging.info(f"Creating uploader for {dest}")
        if dest.startswith("gsapi://"):
            return GsapiUploader(dest[len("gsapi://"):])
        if dest.startswith("gs://"):
            return GsutilUploader(dest)
        if dest.startswith("https://") or dest.startswith("http://"):
            return HttpUploader(dest)
        if ":" in dest:
            return ScpUploader(dest)
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

    @log_timing
    def transfer(self, temp_dir: Path, source: Path):
        logging.info(f"gsapi: uploading to {self.prefix}/{source}")
        if self.prefix == "":
            blob = self.bucket.blob(f"{source}")
        else:
            blob = self.bucket.blob(f"{self.prefix}/{source}")
        blob.upload_from_filename(temp_dir / source)


class GsutilUploader(Uploader):
    def __init__(self, dest: str):
        logging.info(f"gsutil: saving URL {dest}")
        self.url = dest

    @log_timing
    def transfer(self, temp_dir: Path, source: Path):
        logging.info(f"gsutil: cp to {self.url}/{source}")
        subprocess.run(["gsutil/gsutil", "cp",
                        f"{temp_dir / source}",
                        f"{self.url}/{source}"])


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


class ScpUploader(Uploader):
    def __init__(self, dest: str):
        self.host, path = dest.split(":")
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
    starttime: str,
    inputdir: Path,
    tempdir: Path,
    destination: str,
    ccd_list: List[str],
    camera: str,
    compress: bool,
    interval: int,
    numexp: int
):
    hr, min = starttime.split(":")
    seqnum_start = int(hr + min) * 10

    input_path = {ccd: find_input(inputdir, ccd) for ccd in ccd_list}

    uploader = Uploader.create(destination)

    waiter = Waiter(int(hr), int(min), interval)

    now = datetime.now()
    obs_day = now.strftime("%Y%m%d")
    obs_day_str = now.strftime("%Y-%m-%d")

    with tempfile.TemporaryDirectory(dir=tempdir) as temp_dir:
        logging.info(f"Using temp directory {temp_dir}")
        temp_path = Path(temp_dir)
        for i in range(numexp):
            waiter.wait_exposure(i)
            seqnum = seqnum_start + i
            for ccd in ccd_list:
                source_path = input_path[ccd]
                dest_path = Path(obs_day_str).joinpath(
                    f"{obs_day}{seqnum:05d}",
                    f"{camera}_O_{obs_day}_{seqnum:05d}-{ccd}.fits"
                )
                logging.info(f"Copying from {source_path} to"
                             f" {temp_path / dest_path}"
                             f" with compress = {compress}")
                logging.info("Begin copy")
                dest_path = copy(source_path, temp_path, dest_path, compress)
                logging.info("End copy")
                logging.info("Begin transfer")
                uploader.transfer(temp_path, dest_path)
                logging.info("End transfer")


def main():
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        format=f"{args.ccd_list}" + " {asctime} {message}",
        style="{",
        level="INFO"
    )
    simulate(
        args.starttime,
        args.inputdir,
        args.tempdir,
        args.destination,
        args.ccd_list,
        args.camera,
        args.compress,
        args.interval,
        args.numexp,
    )


if __name__ == "__main__":
    main()
