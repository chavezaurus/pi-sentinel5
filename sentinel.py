#!/usr/bin/env python3
import argparse
import ctypes
import errno
import fcntl
import json
import mmap
import os
import select
import sys
import traceback
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from enum import IntEnum
from glob import glob
from math import acos, atan2, cos, degrees, exp, pi, radians, sin, sqrt
from multiprocessing import Event, Pipe, Process, Value
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from shutil import rmtree
from subprocess import Popen
from threading import Thread
from time import (
    CLOCK_MONOTONIC,
    CLOCK_REALTIME,
    clock_gettime,
    localtime,
    sleep,
    strftime,
)

import cherrypy
import ephem
import numpy as np
import serial
import v4l2
from loguru import logger
from PIL import Image
from scipy.optimize import minimize

STORAGE_SIZE = 20 * 1024 * 1024  # 20 MB
STORAGE_NAME = "sentinel_frames"
FULL_WIDTH = 1920
FULL_HEIGHT = 1080
REDUCED_WIDTH = FULL_WIDTH // 2
REDUCED_HEIGHT = FULL_HEIGHT // 2

shared_frame_rate = Value("f", 30.0)
shared_zenith_amplitude = Value("f", 0.0)
shared_state_code = Value("i", 0)


class StateCode(IntEnum):
    IDLE = 0
    WATCHING = 1
    TRIGGERED = 2
    ANALYZING = 3
    COMPOSING = 4
    STARGAZING = 5


@dataclass
class FrameData:
    secs: int = 0
    usecs: int = 0
    offset: int = 0
    size: int = 0
    is_idr: bool = False
    sum: int = 0
    time_string: str = ""
    sequence: int = 0


def state_to_str(code: StateCode) -> str:
    return {
        StateCode.IDLE: "Idle",
        StateCode.WATCHING: "Watching",
        StateCode.TRIGGERED: "Triggered",
        StateCode.ANALYZING: "Analyzing",
        StateCode.COMPOSING: "Composing",
        StateCode.STARGAZING: "Stargazing",
    }[code]


def contains_idr(data, n):
    i = 0
    while i < n - 4:
        # check 4-byte start code
        if data[i : i + 4] == b"\x00\x00\x00\x01":
            header = data[i + 4]
            nal_type = header & 0x1F

            if nal_type == 5:
                return True

            if nal_type == 1:
                return False

            i += 4
            continue

        i += 1

    return False


class Archiver:
    def __init__(self, archive_dir):
        logger.info("Archiver initialized")
        self.state = get_detector_state()
        self.archive_dir = self.state.archivePath
        self.video_file = None
        self.text_file = None
        self.old_minute = "20220101_0000"

    def __del__(self):
        if self.video_file is not None:
            self.video_file.close()
        if self.text_file is not None:
            self.text_file.close()

    def save_frame(self, frameObj, buffer):
        secs = frameObj.secs
        usecs = frameObj.usecs
        timestamp = dateTimeString(secs, usecs)
        minute = timestamp[:13]

        if minute != self.old_minute and frameObj.is_idr:
            self.old_minute = minute
            if self.video_file is not None:
                self.video_file.close()
            if self.text_file is not None:
                self.text_file.close()

            hourString = minute[:11]
            p = Path(self.archive_dir) / f"s{hourString}"
            p.mkdir(parents=True, exist_ok=True)

            video_path = p / f"s{minute}.h264"
            text_path = p / f"s{minute}.txt"

            self.video_file = video_path.open(mode="wb")
            self.text_file = text_path.open(mode="w")

        size = frameObj.size
        if self.video_file is not None:
            self.video_file.write(buffer)
        if self.text_file is not None:
            self.text_file.write(f"{timestamp} {size:7} \n")


def captureProcess(shm, stop_camera, send):
    logger.info("Camera capture started")

    NUM_BUFFERS = 4

    archive = None
    state = get_detector_state()
    device = state.devName
    if state.archivePath.lower() != "none":
        archive = Archiver(state.archivePath)

    fd = os.open(device, os.O_RDWR, 0)

    # -----------------------------
    # Set format
    # -----------------------------
    fmt = v4l2.v4l2_format()
    fmt.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
    fmt.fmt.pix.width = FULL_WIDTH
    fmt.fmt.pix.height = FULL_HEIGHT
    fmt.fmt.pix.pixelformat = v4l2.V4L2_PIX_FMT_H264
    fmt.fmt.pix.field = v4l2.V4L2_FIELD_NONE

    fcntl.ioctl(fd, v4l2.VIDIOC_S_FMT, fmt)

    # -----------------------------
    # Request buffers
    # -----------------------------
    req = v4l2.v4l2_requestbuffers()
    req.count = NUM_BUFFERS
    req.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
    req.memory = v4l2.V4L2_MEMORY_MMAP

    fcntl.ioctl(fd, v4l2.VIDIOC_REQBUFS, req)

    # -----------------------------
    # Map buffers
    # -----------------------------
    buffers = []
    for i in range(req.count):
        buf = v4l2.v4l2_buffer()
        buf.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
        buf.memory = v4l2.V4L2_MEMORY_MMAP
        buf.index = i

        fcntl.ioctl(fd, v4l2.VIDIOC_QUERYBUF, buf)

        mm = mmap.mmap(
            fd,
            buf.length,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=buf.m.offset,
        )

        buffers.append((mm, buf.length))

        # queue buffer
        fcntl.ioctl(fd, v4l2.VIDIOC_QBUF, buf)

    # -----------------------------
    # Start streaming
    # -----------------------------
    buf_type = ctypes.c_int(v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE)
    fcntl.ioctl(fd, v4l2.VIDIOC_STREAMON, buf_type)

    # -----------------------------
    # Capture loop
    # -----------------------------

    shm_buf: memoryview = shm.buf  # type: ignore[assignment]  # buf is non-None right after creation
    frame_count = 0
    offset = 0
    stop_camera.clear()
    while not stop_camera.is_set():
        available = select.select([fd], [], [], 1.0)
        if not available[0]:
            # logger.info("No available camera buffers")
            break

        buf = v4l2.v4l2_buffer()
        buf.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
        buf.memory = v4l2.V4L2_MEMORY_MMAP

        # dequeue
        try:
            fcntl.ioctl(fd, v4l2.VIDIOC_DQBUF, buf)
        except Exception as e:
            logger.error(f"Failed to dequeue buffer: {e}")
            break

        mm, _ = buffers[buf.index]
        n = buf.bytesused
        if n == 0:
            logger.info("Empty buffer received")
            continue

        if offset + n >= STORAGE_SIZE:
            offset = 0
        shm_buf[offset : offset + n] = memoryview(mm)[:n]
        is_idr = contains_idr(mm, n)
        dataObj = FrameData(
            secs=buf.timestamp.secs,
            usecs=buf.timestamp.usecs,
            offset=offset,
            size=n,
            is_idr=is_idr,
        )
        try:
            send.send(dataObj)
        except Exception as e:
            logger.error(f"Failed to put data on pipe: {e}")
            break
        offset += n
        frame_count += 1

        if archive:
            archive.save_frame(dataObj, memoryview(mm)[:n])

        try:
            fcntl.ioctl(fd, v4l2.VIDIOC_QBUF, buf)
        except Exception as e:
            logger.error(f"Failed to queue buffer: {e}")
            break

    # -----------------------------
    # Stop streaming
    # -----------------------------
    fcntl.ioctl(fd, v4l2.VIDIOC_STREAMOFF, buf_type)
    logger.info("Stream off")

    ctrl = v4l2.v4l2_control()
    ctrl.id = v4l2.V4L2_CID_EXPOSURE_AUTO
    ctrl.value = 3  # V4L2_EXPOSURE_MANUAL — restore on exit
    fcntl.ioctl(fd, v4l2.VIDIOC_S_CTRL, ctrl)

    os.close(fd)
    send.send(None)
    # logger.info(f"Capture process stopped, frames captured: {frame_count}")


class CameraSource:
    def __init__(self, shm, stop_camera):
        recv, send = Pipe(duplex=False)
        self.recv = recv
        self.capture_process = Process(
            target=captureProcess,
            args=(
                shm,
                stop_camera,
                send,
            ),
        )
        self.capture_process.start()

    def __del__(self):
        self.capture_process.join()

    def get(self):
        return self.recv.recv()


def decoderProcess(shm, source, detector):
    logger.info("Decoder started")

    DECODER = "/dev/video10"

    BUFFER_COUNT = 4

    frame_count = 0
    shm_buf = memoryview(shm.buf)  # type: ignore[assignment]

    fd = os.open(DECODER, os.O_RDWR)

    #
    # OUTPUT queue: H264 input
    #
    fmt = v4l2.v4l2_format()
    fmt.type = v4l2.V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE

    fmt.fmt.pix_mp.width = FULL_WIDTH
    fmt.fmt.pix_mp.height = FULL_HEIGHT
    fmt.fmt.pix_mp.pixelformat = v4l2.V4L2_PIX_FMT_H264
    fmt.fmt.pix_mp.num_planes = 1

    fcntl.ioctl(fd, v4l2.VIDIOC_S_FMT, fmt)

    #
    # CAPTURE queue: decoded frames
    #
    fmt = v4l2.v4l2_format()
    fmt.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE

    fmt.fmt.pix_mp.width = FULL_WIDTH
    fmt.fmt.pix_mp.height = FULL_HEIGHT
    fmt.fmt.pix_mp.pixelformat = v4l2.V4L2_PIX_FMT_YUV420

    fcntl.ioctl(fd, v4l2.VIDIOC_S_FMT, fmt)

    #
    # Request OUTPUT buffers
    #
    req_out = v4l2.v4l2_requestbuffers()
    req_out.count = BUFFER_COUNT
    req_out.type = v4l2.V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE
    req_out.memory = v4l2.V4L2_MEMORY_MMAP

    fcntl.ioctl(fd, v4l2.VIDIOC_REQBUFS, req_out)

    #
    # Request CAPTURE buffers
    #
    req_cap = v4l2.v4l2_requestbuffers()
    req_cap.count = BUFFER_COUNT
    req_cap.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE
    req_cap.memory = v4l2.V4L2_MEMORY_MMAP

    fcntl.ioctl(fd, v4l2.VIDIOC_REQBUFS, req_cap)

    #
    # Map OUTPUT buffers
    #
    out_buffers = []

    for i in range(req_out.count):
        buf = v4l2.v4l2_buffer()
        planes = (v4l2.v4l2_plane * 1)()

        buf.type = req_out.type
        buf.memory = req_out.memory
        buf.index = i
        buf.length = 1
        buf.m.planes = planes

        fcntl.ioctl(fd, v4l2.VIDIOC_QUERYBUF, buf)

        mm = mmap.mmap(
            fd,
            planes[0].length,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=planes[0].m.mem_offset,
        )

        out_buffers.append(mm)

    #
    # Map CAPTURE buffers
    #
    cap_buffers = []

    for i in range(req_cap.count):
        buf = v4l2.v4l2_buffer()
        planes = (v4l2.v4l2_plane * 1)()

        buf.type = req_cap.type
        buf.memory = req_cap.memory
        buf.index = i
        buf.length = 1
        buf.m.planes = planes

        fcntl.ioctl(fd, v4l2.VIDIOC_QUERYBUF, buf)

        mm = mmap.mmap(
            fd,
            planes[0].length,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=planes[0].m.mem_offset,
        )

        cap_buffers.append(mm)

    #
    # Queue CAPTURE buffers (all empty, waiting for decoded frames)
    #
    for i in range(req_cap.count):
        buf = v4l2.v4l2_buffer()
        planes = (v4l2.v4l2_plane * 1)()

        buf.type = req_cap.type
        buf.memory = req_cap.memory
        buf.index = i
        buf.length = 1
        buf.m.planes = planes

        fcntl.ioctl(fd, v4l2.VIDIOC_QBUF, buf)

    # Pre-fill and queue OUTPUT buffers so the decoder has work to start on
    #
    frameObj = None
    for i in range(req_out.count):
        frameObj = source.get()
        if frameObj is None:
            # logger.info("Stop sentinel")
            wsel = []
            break

        buf = v4l2.v4l2_buffer()
        planes = (v4l2.v4l2_plane * 1)()

        buf.type = req_out.type
        buf.memory = req_out.memory
        buf.index = i
        buf.length = 1
        buf.m.planes = planes
        planes[0].bytesused = frameObj.size

        out_buffers[i][: frameObj.size] = shm_buf[
            frameObj.offset : frameObj.offset + frameObj.size
        ]

        detector.remember(frameObj)
        fcntl.ioctl(fd, v4l2.VIDIOC_QBUF, buf)

    #
    # Start decoder
    #
    fcntl.ioctl(fd, v4l2.VIDIOC_STREAMON, ctypes.c_uint32(req_out.type))
    fcntl.ioctl(fd, v4l2.VIDIOC_STREAMON, ctypes.c_uint32(req_cap.type))

    rsel = [fd]
    wsel = [fd]
    while True:
        r_ready, w_ready, _ = select.select(rsel, wsel, [], 2.0)
        if not r_ready and not w_ready:
            # logger.info("No available decoder buffers")
            break

        if fd in w_ready:
            # OUTPUT buffer returned by decoder: refill and re-queue
            frameObj = source.get()
            if frameObj is None:
                # logger.info("Stop sentinel")
                wsel = []
                continue

            buf = v4l2.v4l2_buffer()
            planes = (v4l2.v4l2_plane * 1)()

            buf.type = req_out.type
            buf.memory = req_out.memory
            buf.length = 1
            buf.m.planes = planes

            fcntl.ioctl(fd, v4l2.VIDIOC_DQBUF, buf)

            out_buffers[buf.index][: frameObj.size] = shm_buf[
                frameObj.offset : frameObj.offset + frameObj.size
            ]

            detector.remember(frameObj)

            buf.timestamp.secs = frameObj.secs
            buf.timestamp.usecs = frameObj.usecs
            planes[0].bytesused = frameObj.size
            fcntl.ioctl(fd, v4l2.VIDIOC_QBUF, buf)

        if fd in r_ready:
            # CAPTURE buffer ready: decoded frame available
            buf = v4l2.v4l2_buffer()
            planes = (v4l2.v4l2_plane * 1)()

            buf.type = req_cap.type
            buf.memory = req_cap.memory
            buf.length = 1
            buf.m.planes = planes

            fcntl.ioctl(fd, v4l2.VIDIOC_DQBUF, buf)
            detector.examine(
                cap_buffers[buf.index], buf.timestamp.secs, buf.timestamp.usecs
            )
            frame_count += 1

            fcntl.ioctl(fd, v4l2.VIDIOC_QBUF, buf)

    detector.finalize()
    fcntl.ioctl(fd, v4l2.VIDIOC_STREAMOFF, ctypes.c_uint32(req_out.type))
    fcntl.ioctl(fd, v4l2.VIDIOC_STREAMOFF, ctypes.c_uint32(req_cap.type))

    os.close(fd)
    # logger.info(f"Decoder stopped, captured {frame_count} frames")


class FileSource:
    def __init__(self, shm, file_path):
        self.shm_buf = memoryview(shm.buf)
        self.file_path = file_path
        self.frame_data = None
        self.extra_frames = 4
        self.frame_count = 0
        self.offset = 0
        self.h264_file = None
        self.text_file = None

    def __enter__(self):
        self.h264_file = Path(self.file_path).with_suffix(".h264").open("rb")
        self.text_file = Path(self.file_path).with_suffix(".txt").open("r")
        return self

    def close(self):
        if self.h264_file is None and self.text_file is None:
            return
        if self.h264_file:
            self.h264_file.close()
            self.h264_file = None
        if self.text_file:
            self.text_file.close()
            self.text_file = None
        # logger.info(f"File reader stopped, captured {self.frame_count} frames")

    def __del__(self):
        self.close()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def get(self):
        if self.h264_file is None or self.text_file is None:
            return None

        line = self.text_file.readline().strip()
        if not line:
            if self.extra_frames > 0:
                self.extra_frames -= 1
                return self.frame_data
            return None

        items = line.split()
        sequence = int(items[0])
        time_string = items[1]
        size = int(items[2])
        data = self.h264_file.read(size)
        is_idr = contains_idr(data, size)

        if self.offset + size >= STORAGE_SIZE:
            self.offset = 0

        self.shm_buf[self.offset : self.offset + size] = data

        self.frame_data = FrameData(
            offset=self.offset,
            size=size,
            is_idr=is_idr,
            sequence=sequence,
            time_string=time_string,
        )

        self.offset += size
        self.frame_count += 1
        return self.frame_data


class ArchiveSource:
    def __init__(self, shm, minute_string):
        hour = minute_string[:-2]
        state = get_detector_state()
        self.h264_path = Path(state.archivePath) / f"s{hour}" / f"s{minute_string}.h264"
        self.text_path = Path(state.archivePath) / f"s{hour}" / f"s{minute_string}.txt"

        self.text_lines = []
        self.text_line_index = 0

        self.half_count = 0
        self.full_count = 0

        self.shm_buf = memoryview(shm.buf)
        self.frame_data = None
        self.extra_frames = 4
        self.offset = 0
        self.h264_file = None

    def __enter__(self):
        self.h264_file = open(self.h264_path, "rb")
        with self.text_path.open() as f:
            self.text_lines = f.readlines()
            self.half_count = len(self.text_lines) // 2
            self.full_count = self.half_count * 2
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.h264_file is not None:
            self.h264_file.close()

    def __del__(self):
        if self.h264_file is not None:
            self.h264_file.close()

    def get(self):
        if self.h264_file is None:
            return None

        if self.text_line_index >= self.full_count:
            if self.extra_frames > 0:
                self.extra_frames -= 1
                return self.frame_data
            return None

        line = self.text_lines[self.text_line_index]

        items = line.split()
        size = int(items[1])
        data = self.h264_file.read(size)
        is_idr = contains_idr(data, size)

        if self.offset + size >= STORAGE_SIZE:
            self.offset = 0

        self.shm_buf[self.offset : self.offset + size] = data

        self.frame_data = FrameData(
            offset=self.offset,
            size=size,
            is_idr=is_idr,
            secs=1 if self.text_line_index < self.half_count else -1,
        )

        self.offset += size
        self.text_line_index += 1

        return self.frame_data


def raw_mask():
    try:
        img = Image.open("mask.jpg").convert("RGB")
    except FileNotFoundError:
        logger.warning("mask.jpg not found")
        return np.full((FULL_HEIGHT, FULL_WIDTH), 0, dtype=np.uint8)

    if img.size != (FULL_WIDTH, FULL_HEIGHT):
        img = img.resize(
            (FULL_WIDTH, FULL_HEIGHT), Image.Resampling.LANCZOS
        )  # Use a high-quality filter for best results

    imageArray = np.array(img)
    red_channel = imageArray[:, :, 0]
    green_channel = imageArray[:, :, 1]
    blue_channel = imageArray[:, :, 2]

    mask = (red_channel > 230) & (green_channel < 20) & (blue_channel < 20)
    mask = np.where(mask, 255, 0).astype(np.uint8)
    return mask


def get_mask(full_size=False):
    detector_state = get_detector_state()
    noise_threshold = detector_state.noiseThreshold
    width = REDUCED_WIDTH if not full_size else FULL_WIDTH
    height = REDUCED_HEIGHT if not full_size else FULL_HEIGHT

    try:
        img = Image.open("mask.jpg").convert("RGB")
    except FileNotFoundError:
        logger.warning("mask.jpg not found")
        return np.full((height, width), noise_threshold, dtype=np.int16)

    if img.size != (width, height):
        img = img.resize(
            (width, height), Image.Resampling.LANCZOS
        )  # Use a high-quality filter for best results

    imageArray = np.array(img)
    red_channel = imageArray[:, :, 0]
    green_channel = imageArray[:, :, 1]
    blue_channel = imageArray[:, :, 2]

    mask = (red_channel > 230) & (green_channel < 20) & (blue_channel < 20)
    mask = np.where(mask, 255, noise_threshold).astype(np.int16)
    return mask


@dataclass
class DetectorState:
    archivePath: str = "None"
    devName: str = "/dev/video2"
    eventsPerHour: int = 10
    noiseThreshold: int = 50
    sumThreshold: int = 50


def get_detector_state():
    filepath = Path("state.json")
    if not filepath.exists():
        return DetectorState()

    # Open the file and load the JSON data
    dict = {}
    with filepath.open() as file:
        dict = json.load(file)
        dstate = DetectorState(
            archivePath=dict.get("archivePath", "None"),
            devName=dict.get("devName", "/dev/video2"),
            eventsPerHour=dict.get("eventsPerHour", 10),
            noiseThreshold=dict.get("noiseThreshold", 50),
            sumThreshold=dict.get("sumThreshold", 50),
        )
    return dstate


def dateTimeString(secs, usecs):
    monotonic_to_realtime = clock_gettime(CLOCK_REALTIME) - clock_gettime(
        CLOCK_MONOTONIC
    )
    dt = datetime.fromtimestamp(
        secs + usecs / 1_000_000 + monotonic_to_realtime
    ).astimezone(UTC)
    return dt.strftime("%Y%m%d_%H%M%S_%f")[:-3]


def yuv_to_rgb(yuv):
    """
    Convert YUV (YCbCr) to RGB
    """
    Y = yuv[..., 0].astype(np.float32)
    U = yuv[..., 1].astype(np.float32) - 128
    V = yuv[..., 2].astype(np.float32) - 128

    R = Y + 1.402 * V
    G = Y - 0.344136 * U - 0.714136 * V
    B = Y + 1.772 * U

    rgb = np.stack([R, G, B], axis=-1)

    return np.clip(rgb, 0, 255).astype(np.uint8)


@dataclass
class Calibration:
    V: float = 0.002278
    S: float = 0.0
    D: float = 0.0
    a0: float = 0.0
    E: float = 0.0
    eps: float = 0.0
    COPx: float = 960.0
    COPy: float = 540.0
    alpha: float = 0.0
    flat: float = 0.0
    cameraLatitude: float = 0.0
    cameraLongitude: float = 0.0
    cameraElevation: float = 0.0


def get_calibration():
    filepath = Path("calibration.json")
    if not filepath.exists():
        return Calibration()

    try:
        with open(filepath) as file:
            dict = json.load(file)
            return Calibration(**dict)
    except FileNotFoundError:
        return Calibration()
    except json.JSONDecodeError as e:
        logger.warning(f"Error: Invalid JSON format in '{filepath}': {e}")
        return Calibration()


class Converter:
    def __init__(self, cal):
        self.cal = cal

        alpha = cal.alpha
        flat = cal.flat
        COPx = cal.COPx
        COPy = cal.COPy

        dilation = sqrt(1.0 - flat)
        cos_alpha = cos(alpha)
        sin_alpha = sin(alpha)

        K = COPx * sin_alpha + COPy * cos_alpha
        L = COPy * sin_alpha - COPx * cos_alpha

        self.c = cos_alpha * cos_alpha * dilation + sin_alpha * sin_alpha / dilation
        self.d = sin_alpha * cos_alpha * dilation - sin_alpha * cos_alpha / dilation
        self.e = (
            -(K * cos_alpha * dilation * dilation - COPy * dilation + L * sin_alpha)
            / dilation
        )
        self.f = sin_alpha * cos_alpha * dilation - sin_alpha * cos_alpha / dilation
        self.g = sin_alpha * sin_alpha * dilation + cos_alpha * cos_alpha / dilation
        self.h = (
            -(K * sin_alpha * dilation * dilation - COPx * dilation - L * cos_alpha)
            / dilation
        )

    def convert(self, px, py):
        pxt = self.g * px + self.f * py + self.h
        pyt = self.d * px + self.c * py + self.e

        x = pxt - self.cal.COPx
        y = pyt - self.cal.COPy

        a0 = radians(self.cal.a0)
        eps = radians(self.cal.eps)
        E = radians(self.cal.E)

        r = sqrt(x * x + y * y)
        u = self.cal.V * r + self.cal.S * (exp(self.cal.D * r) - 1)
        b = a0 - E + atan2(x, y)

        angle = b
        z = u

        if eps != 0.0:
            z = acos(cos(u) * cos(eps) - sin(u) * sin(eps) * cos(b))
            sinAngle = sin(b) * sin(u) / sin(z)
            cosAngle = (cos(u) - cos(eps) * cos(z)) / (sin(eps) * sin(z))
            angle = atan2(sinAngle, cosAngle)

        elev = pi / 2 - z
        azim = angle + E - pi

        azim = degrees(azim)
        elev = degrees(elev)

        while azim >= 360.0:
            azim -= 360.0
        while azim < 0.0:
            azim += 360.0

        return azim, elev


class Analyzer:
    MEM_SIZE = 100

    def __init__(self, file_name):
        logger.info(f"Analyzer process started: {file_name}")

        self.memory = [FrameData()] * self.MEM_SIZE
        self.memory_index = 0
        self.examine_index = 0
        self.mask = get_mask(full_size=True)
        self.reference = np.zeros((FULL_HEIGHT, FULL_WIDTH), dtype=np.int16)
        self.center_col = 0
        self.center_row = 0
        self.azim = 0
        self.elev = 0
        self.first = True

        self.converter = Converter(get_calibration())
        self.file = Path(file_name).with_suffix(".csv").open("w")

    def __del__(self):
        if self.file:
            self.file.close()

    def remember(self, frame_data):
        self.memory[self.memory_index] = frame_data
        self.memory_index = (self.memory_index + 1) % self.MEM_SIZE

    def finalize(self):
        if self.file:
            self.file.close()
            logger.info(f"Analyzer process stopped: {self.file.name}")
        self.file = None

    def examine(self, frameBuf, secs, usecs):
        Y = (
            np.frombuffer(frameBuf[: FULL_HEIGHT * FULL_WIDTH], dtype=np.uint8)
            .reshape(FULL_HEIGHT, FULL_WIDTH)
            .astype(np.int16)
        )

        if self.first:
            self.reference = Y
            self.first = False

        test = Y - self.reference - self.mask
        np.clip(test, 0, None, out=test)

        self.reference *= 15
        self.reference += Y
        self.reference >>= 4

        self.reference = np.maximum(self.reference, Y)

        # Get the total mass (sum of all elements in the array)
        total_mass = np.sum(test)
        non_zero_count = np.count_nonzero(test)

        frame_data = self.memory[self.examine_index]
        self.examine_index = (self.examine_index + 1) % self.MEM_SIZE

        if total_mass == 0:
            self.center_col = 0
            self.center_row = 0
            self.azim = 0
            self.elev = 0
        else:
            # Create coordinate grids
            # np.ogrid is memory efficient for larger arrays
            # The coordinates for rows (y) and columns (x)
            y_coords, x_coords = np.ogrid[: test.shape[0], : test.shape[1]]

            # Calculate the center of mass coordinates
            # Element-wise multiplication of coordinates with masses, then sum,
            # then divide by total mass
            self.center_col = np.sum(x_coords * test) / total_mass
            self.center_row = np.sum(y_coords * test) / total_mass

            self.azim, self.elev = self.converter.convert(
                self.center_col, self.center_row
            )

        if self.file:
            self.file.write(
                f"{frame_data.time_string},{non_zero_count:6},{total_mass:8},"
            )
            self.file.write(f"{self.center_col:10.1f},{self.center_row:10.1f},")
            self.file.write(f"{self.azim:10.1f},{self.elev:10.1f}\n")


class StarFinder:
    def __init__(self, minute_string):
        logger.info(f"Star finder process started: {minute_string}")

        self.Y = np.zeros((FULL_HEIGHT, FULL_WIDTH), dtype=np.int32)
        self.file_path = Path("calibration") / f"a{minute_string}00_000.jpg"
        self.count = 0

    def __del__(self):
        logger.info(f"Star finder process stopped: {self.file_path}")

    def remember(self, frame_data):
        pass

    def examine(self, frameBuf, secs, usecs):
        frame = np.frombuffer(
            frameBuf[: FULL_HEIGHT * FULL_WIDTH], dtype=np.uint8
        ).reshape((FULL_HEIGHT, FULL_WIDTH))
        if secs > 0:
            self.Y += frame
            self.count += 1
        elif secs < 0:
            self.Y -= frame
            self.count += 1

    def finalize(self):
        if self.count != 0:
            self.Y *= 200
            self.Y //= self.count
        self.Y = np.clip(self.Y, 0, 250).astype(np.uint8)
        grayscale_image = Image.fromarray(self.Y, mode="L")
        grayscale_image.save(self.file_path)


class Composer:
    def __init__(self, file_path):
        logger.info(f"Composer process started: {file_path}")
        self.best_Y = np.full((FULL_HEIGHT, FULL_WIDTH), 0, dtype=np.uint8)
        self.best_U = np.full((FULL_HEIGHT // 2, FULL_WIDTH // 2), 0, dtype=np.uint8)
        self.best_V = np.full((FULL_HEIGHT // 2, FULL_WIDTH // 2), 0, dtype=np.uint8)
        self.file_path = file_path
        self.count = 0

    def __del__(self):
        jpeg_path = Path(self.file_path).with_suffix(".jpg")
        logger.info(f"Composer process stopped: {jpeg_path}")

    def update(self, Y, U, V):
        """
        Y: (H, W)
        U, V: (H//2, W//2)
        """

        # Step 1: find brighter pixels
        mask = Y >= self.best_Y

        # Step 2: update Y
        self.best_Y[mask] = Y[mask]

        # Step 3: propagate mask to U/V resolution
        # reshape into 2x2 blocks
        H2, W2 = FULL_HEIGHT // 2, FULL_WIDTH // 2

        block_mask = mask.reshape(H2, 2, W2, 2)

        # If ANY pixel in the 2x2 block improved → update chroma
        uv_mask = block_mask.any(axis=(1, 3))  # shape (H/2, W/2)

        # Only update if most pixels improved
        # uv_mask = (block_mask.sum(axis=(1, 3)) >= 2)

        # Best for detecting brief highlights (e.g., sparks, motion)
        # uv_mask = block_mask.any(axis=(1, 3))

        # Slightly less accurate, but very fast
        # uv_mask = mask[::2, ::2]

        # Step 4: update U/V
        self.best_U[uv_mask] = U[uv_mask]
        self.best_V[uv_mask] = V[uv_mask]

    def examine(self, frameBuf, secs, usecs):
        self.count += 1

        Y = np.frombuffer(frameBuf[: FULL_HEIGHT * FULL_WIDTH], dtype=np.uint8).reshape(
            FULL_HEIGHT, FULL_WIDTH
        )

        u_start = (FULL_HEIGHT + 8) * FULL_WIDTH
        u_end = u_start + FULL_HEIGHT * FULL_WIDTH // 4
        v_start = u_start + (FULL_HEIGHT + 8) * FULL_WIDTH // 4
        v_end = v_start + FULL_HEIGHT * FULL_WIDTH // 4

        U = np.frombuffer(frameBuf[u_start:u_end], dtype=np.uint8).reshape(
            (FULL_HEIGHT // 2, FULL_WIDTH // 2)
        )

        V = np.frombuffer(frameBuf[v_start:v_end], dtype=np.uint8).reshape(
            (FULL_HEIGHT // 2, FULL_WIDTH // 2)
        )

        self.update(Y, U, V)

    def get_result(self):
        return self.best_Y, self.best_U, self.best_V

    def upsample_uv(self, U, V):
        return (
            U.repeat(2, axis=0).repeat(2, axis=1),
            V.repeat(2, axis=0).repeat(2, axis=1),
        )

    def makeOverlay(self):
        mask = raw_mask()
        Y, U, V = self.get_result()
        reduced_mask = mask[::2, ::2]
        Y_brightened = np.where(Y + 10 > 254, 254, Y + 10)
        Y_overlay = np.where(mask == 255, Y_brightened, Y)
        U_overlay = np.where(reduced_mask == 255, 116, U)
        V_overlay = np.where(reduced_mask == 255, 140, V)

        U_up, V_up = self.upsample_uv(U_overlay, V_overlay)

        rgb = yuv_to_rgb(np.stack([Y_overlay, U_up, V_up], axis=-1))

        img = Image.fromarray(rgb)
        path = Path(self.file_path)
        mpath = path.with_name(f"{path.stem}m.jpg")
        img.save(mpath)

    def finalize(self):
        Y, U, V = self.get_result()
        U_up, V_up = self.upsample_uv(U, V)

        rgb = yuv_to_rgb(np.stack([Y, U_up, V_up], axis=-1))

        img = Image.fromarray(rgb)
        img.save(Path(self.file_path).with_suffix(".jpg"))

        self.makeOverlay()

    def remember(self, frame_data):
        pass


class Stub:
    def __init__(self):
        self.remember_count = 0
        self.examine_count = 0

    def remember(self, frame_data):
        self.remember_count += 1
        logger.info(f"remember_count={self.remember_count}")
        pass

    def examine(self, frameBuf, secs, usecs):
        self.examine_count += 1
        logger.info(f"examine_count={self.examine_count}")
        pass


class Detector:
    MEM_SIZE = 150
    FRAMES_PER_HOUR = 60 * 60 * 30
    STRIDE = 3

    def __init__(self, shm, force_trigger_event):
        logger.info("Detector process started")

        self.memory = [FrameData()] * self.MEM_SIZE
        self.memory_index = 0
        self.state = get_detector_state()
        self.mask = get_mask()
        self.shm = shm
        self.selmask = np.full((REDUCED_HEIGHT, REDUCED_WIDTH), 50, dtype=np.int16)
        self.reference = np.full((REDUCED_HEIGHT, REDUCED_WIDTH), 255, dtype=np.int16)
        self.triggered = False
        self.untriggered = False
        self.trigger_count = 0
        self.event_duration = 0
        self.untrigger_count = 0
        self.rate_limit_bank = self.FRAMES_PER_HOUR
        self.trigger_time = (0, 0)
        self.video_file = None
        self.text_file = None
        self.search_index = 0
        self.frame_offset = 0
        self.frame_rate = 30.0
        self.zenith_amplitude = 0.0
        self.previous_time = 0.0
        self.force_trigger_event = force_trigger_event

    def __del__(self):
        logger.info("Detector process stopped")

    def remember(self, frame_data):
        self.memory[self.memory_index] = frame_data
        self.memory_index = (self.memory_index + 1) % self.MEM_SIZE

    def finalize(self):
        if self.video_file:
            self.video_file.close()
        if self.text_file:
            self.text_file.close()

    def measure_frame_rate(self, secs, usecs):
        current_time = secs + usecs / 1e6
        elapsed = current_time - self.previous_time
        if elapsed == 0.0:
            return
        frame_rate = 1.0 / elapsed
        self.frame_rate = 0.99 * self.frame_rate + 0.01 * frame_rate
        self.previous_time = current_time
        shared_frame_rate.value = self.frame_rate

    def measure_zenith_amplitude(self, frame):
        amplitude = frame[REDUCED_WIDTH // 2, REDUCED_HEIGHT // 2]
        self.zenith_amplitude = 0.99 * self.zenith_amplitude + 0.01 * amplitude
        shared_zenith_amplitude.value = self.zenith_amplitude

    def examine(self, frameBuf, secs, usecs):
        frame = (
            np.frombuffer(frameBuf[: FULL_HEIGHT * FULL_WIDTH], dtype=np.uint8)
            .reshape(FULL_HEIGHT, FULL_WIDTH)[1:FULL_HEIGHT:2, 1:FULL_WIDTH:2]
            .astype(np.int16)
        )

        test = frame - self.reference - self.mask
        np.clip(test, 0, None, out=test)
        sum = np.sum(test)

        self.reference *= 15
        self.reference += frame
        self.reference >>= 4

        self.reference = np.maximum(self.reference, frame)

        if sum > 0:
            search_index = self.memory_index
            for _ in range(10):
                search_index = (search_index - 1) % self.MEM_SIZE
                dataObj: FrameData = self.memory[search_index]
                test_secs = dataObj.secs
                test_usecs = dataObj.usecs
                if test_secs == secs and test_usecs == usecs:
                    dataObj.sum = int(sum)
                    break

        self.detect(sum, secs, usecs)
        self.measure_frame_rate(secs, usecs)
        self.measure_zenith_amplitude(frame)

    def initiateTrigger(self, secs, usecs):
        dt = dateTimeString(secs, usecs)
        logger.info(f"Trigger event at: {dt}")
        try:
            self.video_file = open(f"new/s{dt}.h264", "wb")
            self.text_file = open(f"new/s{dt}.txt", "w")
        except Exception as e:
            logger.error(f"Failed to open files: {e}")
            return

        shared_state_code.value = StateCode.TRIGGERED
        self.triggered = True
        self.untriggered = False
        self.event_duration = 0
        self.untrigger_count = 0
        self.trigger_time = (secs, usecs)

        self.search_index = self.memory_index
        self.frame_offset = 0

        self.search_index = self.memory_index
        for _ in range(self.MEM_SIZE):
            self.search_index = (self.search_index - 1) % self.MEM_SIZE
            frameObj = self.memory[self.search_index]
            if frameObj.secs == secs and frameObj.usecs == usecs:
                self.frame_offset = 0
            else:
                self.frame_offset -= 1
            if frameObj.is_idr and self.frame_offset < -25:
                break

        self.continueTrigger(secs, usecs)

    def continueTrigger(self, secs, usecs):
        if not self.video_file or not self.text_file:
            return

        for _ in range(self.MEM_SIZE):
            frameObj = self.memory[self.search_index]
            if frameObj.secs == secs and frameObj.usecs == usecs:
                break

            segment = self.shm.buf[frameObj.offset : frameObj.offset + frameObj.size]  # type: ignore[assignment]
            self.video_file.write(segment)
            self.text_file.write(
                f"{self.frame_offset:4} "
                f"{dateTimeString(frameObj.secs, frameObj.usecs)} "
                f"{frameObj.size:7} {frameObj.sum:7}\n"
            )

            self.search_index = (self.search_index + 1) % self.MEM_SIZE
            self.frame_offset += 1

    def terminateTrigger(self, secs, usecs):
        self.continueTrigger(secs, usecs)
        self.triggered = False
        self.untriggered = False
        self.event_duration = 0
        self.untrigger_count = 0

        self.rate_limit_bank = max(
            0, self.rate_limit_bank - self.FRAMES_PER_HOUR // self.state.eventsPerHour
        )

        if self.video_file:
            self.video_file.close()
        if self.text_file:
            self.text_file.close()
        dt = dateTimeString(self.trigger_time[0], self.trigger_time[1])
        dt_end = dateTimeString(secs, usecs)
        logger.info(f"Terminate event at: {dt_end}")
        h264_path = f"new/s{dt}.h264"
        mp4_path = f"new/s{dt}.mp4"
        Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-r",
                str(self.frame_rate),
                "-i",
                h264_path,
                "-vcodec",
                "copy",
                mp4_path,
            ]
        )
        self.video_file = None
        self.text_file = None
        shared_state_code.value = StateCode.WATCHING

    def detect(self, sum, secs, usecs):
        self.rate_limit_bank = min(self.rate_limit_bank + 1, self.FRAMES_PER_HOUR)

        if not self.triggered:
            tooMany = (
                self.state.eventsPerHour * self.rate_limit_bank // self.FRAMES_PER_HOUR
                == 0
            )

            if self.force_trigger_event.is_set():
                self.initiateTrigger(secs, usecs)
                self.force_trigger_event.clear()
            elif sum > self.state.sumThreshold and not tooMany:
                self.trigger_count += 1
                if self.trigger_count >= 2:
                    self.initiateTrigger(secs, usecs)
            else:
                self.trigger_count = 0
        elif self.triggered and not self.untriggered:
            self.event_duration += 1
            self.continueTrigger(secs, usecs)

            if sum < self.state.sumThreshold:
                self.untrigger_count += 1
                if self.untrigger_count >= 2:
                    self.untriggered = True
            else:
                self.untrigger_count = 0
                if self.event_duration > 300:
                    self.terminateTrigger(secs, usecs)
        else:
            self.event_duration += 1
            self.continueTrigger(secs, usecs)

            if sum < self.state.sumThreshold:
                self.untrigger_count += 1
            if self.event_duration > 60 and self.untrigger_count >= 15:
                self.terminateTrigger(secs, usecs)
            elif self.event_duration > 600:
                self.terminateTrigger(secs, usecs)


class SentinelServer:
    MAX_PERCENT_USAGE = 90.0

    def __init__(self, shm):
        logger.info("init SentinelServer")
        self.startUTC = {"h": 21, "m": 30}
        self.stopUTC = {"h": 5, "m": 0}
        self.analyze_and_compose = False
        self.stop_camera_event = Event()
        self.force_trigger_event = Event()
        self.archivePath = "None"
        self.devName = "/dev/video2"

        self.gpsModule = GPS_Module()

        Path("new").mkdir(exist_ok=True)
        Path("trash").mkdir(exist_ok=True)
        Path("saved").mkdir(exist_ok=True)
        Path("calibration").mkdir(exist_ok=True)

        self.shm = shm

    def __del__(self):
        logger.info("SentinelServer stopped")

    def CalibrationVector(self, obj):
        v = [
            obj["V"],
            obj["S"],
            obj["D"],
            radians(obj["a0"]),
            radians(obj["E"]),
            radians(obj["eps"]),
            obj["COPx"],
            obj["COPy"],
            obj["alpha"],
            obj["flat"],
        ]
        return v

    def CalibrationObject(self, v):
        obj = {
            "V": v[0],
            "S": v[1],
            "D": v[2],
            "a0": degrees(v[3]),
            "E": degrees(v[4]),
            "eps": degrees(v[5]),
            "COPx": v[6],
            "COPy": v[7],
            "alpha": v[8],
            "flat": v[9],
        }
        return obj

    def CalibrationTruncate(self, obj):
        obj["V"] = round(obj["V"], 6)
        obj["S"] = round(obj["S"], 6)
        obj["D"] = round(obj["D"], 6)
        obj["a0"] = round(obj["a0"], 3)
        obj["E"] = round(obj["E"], 3)
        obj["eps"] = round(obj["eps"], 3)
        obj["COPx"] = round(obj["COPx"], 3)
        obj["COPy"] = round(obj["COPy"], 3)
        obj["alpha"] = round(obj["alpha"], 6)
        obj["flat"] = round(obj["flat"], 6)

    def getEvents(self, directory):
        jpgSet = set()
        mpgSet = set()

        pathDir = Path(directory)
        for path in pathDir.glob("s*[0-9].jpg"):
            jpgSet.add(path.stem)
        for path in pathDir.glob("s*.mp4"):
            mpgSet.add(path.stem)

        eventSet = jpgSet | mpgSet

        eventList = list(eventSet)
        eventList.sort()

        events = [
            {
                "event": e,
                "to": directory,
                "from": directory,
                "j": e in jpgSet,
                "m": e in mpgSet,
            }
            for e in eventList
        ]

        return events

    def relocateCurrent(self, currentData):
        for item in currentData:
            dfrom = item["from"]
            dto = item["to"]

            if dfrom != dto:
                root = Path(item["event"]).stem
                fromPath = Path(dfrom) / root
                toPath = Path(dto) / root

                mp4_file = fromPath.with_suffix(".mp4")
                if mp4_file.exists():
                    mp4_file.rename(toPath.with_suffix(".mp4"))

                h264_file = fromPath.with_suffix(".h264")
                if h264_file.exists():
                    h264_file.rename(toPath.with_suffix(".h264"))

                txt_file = fromPath.with_suffix(".txt")
                if txt_file.exists():
                    txt_file.rename(toPath.with_suffix(".txt"))

                jpg_file = fromPath.with_suffix(".jpg")
                if jpg_file.exists():
                    jpg_file.rename(toPath.with_suffix(".jpg"))

                csv_file = fromPath.with_suffix(".csv")
                if csv_file.exists():
                    csv_file.rename(toPath.with_suffix(".csv"))

                mjpg_file = fromPath.with_name(f"{fromPath.stem}m.jpg")
                if mjpg_file.exists():
                    mjpg_file.rename(toPath.with_name(f"{toPath.stem}m.jpg"))

    def TotalCalibrationError(self, calVector, skyList):
        obj = self.CalibrationObject(calVector)
        converter = Converter(obj)

        sum = 0.0
        for skyThing in skyList:
            px = skyThing["px"]
            py = skyThing["py"]
            azim = skyThing["azim"]
            elev = skyThing["elev"]

            azim_s, elev_s = converter.convert(px, py)

            x1 = cos(radians(azim)) * cos(radians(azim))
            y1 = sin(radians(azim)) * cos(radians(elev))

            x2 = cos(radians(azim_s)) * cos(radians(azim_s))
            y2 = sin(radians(azim_s)) * cos(radians(elev_s))

            dx = x1 - x2
            dy = y1 - y2

            sum += sqrt(dx * dx + dy * dy)

        return sum

    def runStopSequence(self):
        self.stop_camera_event.set()

    def handle_set_state(self, data):
        if "startUTC" in data:
            self.startUTC = data["startUTC"]
        if "stopUTC" in data:
            self.stopUTC = data["stopUTC"]
        if "gpsLatitude" in data:
            self.gpsLatitude = data["gpsLatitude"]
        if "gpsLongitude" in data:
            self.gpsLongitude = data["gpsLongitude"]
        if "devName" in data:
            self.devName = data["devName"]
        if "archivePath" in data:
            self.archivePath = data["archivePath"]

    def checkExposure(self):
        frame_rate = shared_frame_rate.value
        if frame_rate < 17.0:
            fd = os.open(self.devName, os.O_RDWR)
            ctrl = v4l2.v4l2_control()
            ctrl.id = v4l2.V4L2_CID_EXPOSURE_AUTO
            ctrl.value = 1
            fcntl.ioctl(fd, v4l2.VIDIOC_S_CTRL, ctrl)
            ctrl.id = v4l2.V4L2_CID_EXPOSURE_ABSOLUTE
            ctrl.value = 333
            fcntl.ioctl(fd, v4l2.VIDIOC_S_CTRL, ctrl)
            ctrl.id = v4l2.V4L2_CID_GAIN
            ctrl.value = 0
            fcntl.ioctl(fd, v4l2.VIDIOC_S_CTRL, ctrl)
            os.close(fd)

            logger.info(
                f"Fixed exposure at: {strftime('%Y-%m-%d %H:%M:%S', localtime())}"
            )
            return

        zenith_amplitude = shared_zenith_amplitude.value
        if shared_state_code.value == StateCode.WATCHING and zenith_amplitude > 230.0:
            logger.info(
                f"Auto exposure at: {strftime('%Y-%m-%d %H:%M:%S', localtime())}"
            )
            fd = os.open(self.devName, os.O_RDWR)
            ctrl = v4l2.v4l2_control()
            ctrl.id = v4l2.V4L2_CID_EXPOSURE_AUTO
            ctrl.value = 3  # V4L2_EXPOSURE_APERTURE_PRIORITY — restore auto on exit
            fcntl.ioctl(fd, v4l2.VIDIOC_S_CTRL, ctrl)
            os.close(fd)

    def pruneArchive(self):
        stat = os.statvfs(self.archivePath)
        usage = 100.0 * (1.0 - stat.f_bavail / stat.f_blocks)
        # print("Percent usage: %7.1f" % usage)
        if usage > self.MAX_PERCENT_USAGE:
            lst = [d for d in os.listdir(self.archivePath) if d.startswith("s")]
            lst.sort()
            if len(lst) >= 2:
                logger.info(f"Pruning archive: {lst[0]}")
                rmtree(os.path.join(self.archivePath, lst[0]))

    def backgroundAnalyzeAndCompose(self):
        # Only do this if we are not busy
        if shared_state_code.value != StateCode.IDLE.value:
            return

        self.analyze_and_compose = False
        files = Path("new").glob("*.h264")
        for file in files:
            csv_path = file.with_suffix(".csv")
            jpg_path = file.with_suffix(".jpg")
            if not csv_path.exists():
                Thread(target=self.analyzeEvent, daemon=True, args=(file,)).start()
                self.analyze_and_compose = True
                return

            if not jpg_path.exists():
                Thread(target=self.composeEvent, daemon=True, args=(file,)).start()
                self.analyze_and_compose = True
                return

    def backgroundProcess(self):
        # Wait for engine to start up
        while cherrypy.engine.state != cherrypy.engine.states.STARTED:  # type: ignore[attr-defined]
            sleep(1)

        last_time = time(hour=0, minute=0)
        path = Path("state.json")
        if path.exists():
            with path.open() as f:
                s = f.read()
                data = json.loads(s)
                self.handle_set_state(data)

        while cherrypy.engine.state == cherrypy.engine.states.STARTED:  # type: ignore[attr-defined]
            # Check for timed actions
            utc_now = datetime.now(UTC).time()
            utc_now = utc_now.replace(second=0, microsecond=0)
            if utc_now != last_time:
                tstart = time(hour=self.startUTC["h"], minute=self.startUTC["m"])
                tstop = time(hour=self.stopUTC["h"], minute=self.stopUTC["m"])

                if tstart == utc_now and tstart != tstop:
                    if shared_state_code.value == StateCode.IDLE.value:
                        shared_state_code.value = StateCode.WATCHING.value
                        Thread(target=self.runCamera, daemon=True).start()
                elif tstop == utc_now and tstart != tstop:
                    if shared_state_code.value == StateCode.WATCHING.value:
                        self.runStopSequence()
                        self.analyze_and_compose = True
                else:
                    self.checkExposure()

                # print(f"Time: {utc_now} Start: {tstart} Stop: {tstop}")
                last_time = utc_now

                if self.archivePath.lower() != "none":
                    self.pruneArchive()

            if self.analyze_and_compose:
                self.backgroundAnalyzeAndCompose()

            sleep(2)

    def runCamera(self):
        shared_state_code.value = StateCode.WATCHING
        source = CameraSource(self.shm, self.stop_camera_event)
        detector = Detector(self.shm, self.force_trigger_event)
        p = Process(target=decoderProcess, args=(self.shm, source, detector))
        p.start()
        p.join()
        shared_state_code.value = StateCode.IDLE

    def analyzeEvent(self, file_path):
        shared_state_code.value = StateCode.ANALYZING
        with FileSource(self.shm, file_path) as source:
            analyzer = Analyzer(file_path)
            p = Process(target=decoderProcess, args=(self.shm, source, analyzer))
            p.start()
            p.join()
        shared_state_code.value = StateCode.IDLE

    def composeEvent(self, file_path):
        shared_state_code.value = StateCode.COMPOSING
        with FileSource(self.shm, file_path) as source:
            composer = Composer(file_path)
            p = Process(target=decoderProcess, args=(self.shm, source, composer))
            p.start()
            p.join()
        shared_state_code.value = StateCode.IDLE

    def averageEvent(self, minute_string):
        shared_state_code.value = StateCode.STARGAZING
        with ArchiveSource(self.shm, minute_string) as source:
            starFinder = StarFinder(minute_string)
            p = Process(target=decoderProcess, args=(self.shm, source, starFinder))
            p.start()
            p.join()
        shared_state_code.value = StateCode.IDLE

    @cherrypy.expose
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def subscribe(self):
        current_state = shared_state_code.value
        for _ in range(20):
            if shared_state_code.value != current_state:
                logger.info(f"State code changed: {shared_state_code.value}")
                return {"msg": "changed"}
            sleep(1)
        return {"msg": ""}

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def playback(self):
        detector_state = get_detector_state()
        if detector_state.archivePath.lower() == "none":
            return {"response": "No archive path"}

        data = cherrypy.request.json
        utcTime = datetime.fromtimestamp(data["timestamp"] / 1000, tz=UTC)

        tm = utcTime - timedelta(seconds=2)
        tp = utcTime + timedelta(seconds=data["duration"] + 1)

        tm_string = tm.strftime("%Y%m%d_%H%M%S_000")
        tp_string = tp.strftime("%Y%m%d_%H%M%S_000")

        file_set = set()

        while tm < tp + timedelta(seconds=60):
            path0 = (
                Path(detector_state.archivePath)
                / tm.strftime("s%Y%m%d_%H")
                / tm.strftime("s%Y%m%d_%H%M.txt")
            )
            if path0.exists():
                file_set.add(path0)
            tm = tm + timedelta(seconds=60)

        file_list = sorted(file_set)

        if len(file_list) == 0:
            return {"response": "Archive file not found"}

        playbackPath = Path(utcTime.strftime("new/s%Y%m%d_%H%M%S_000.h264"))

        playback_list = []
        for path in file_list:
            obj = {"path": path, "offset": 0, "frames": []}
            with path.open() as ft:
                for line in ft:
                    items = line.split()
                    if len(items) != 2:
                        continue
                    ftime = items[0]
                    fsize = int(items[1])
                    if ftime < tm_string:
                        obj["offset"] += fsize
                    elif ftime >= tp_string:
                        break
                    else:
                        obj["frames"].append((ftime, fsize))
            if len(obj["frames"]) >= 30:
                playback_list.append(obj)

        with (
            playbackPath.open("wb") as pv,
            playbackPath.with_suffix(".txt").open("w") as pt,
        ):
            idr_found = False
            count = 0
            for obj in playback_list:
                vpath = obj["path"].with_suffix(".h264")
                tpath = vpath.with_suffix(".txt")
                with vpath.open("rb") as fv, tpath.open("r") as ft:
                    if obj["offset"] != 0:
                        fv.seek(obj["offset"], 0)
                    for frame in obj["frames"]:
                        ftime, fsize = frame
                        video_data = fv.read(fsize)
                        idr_found = idr_found or contains_idr(video_data, fsize)
                        if idr_found:
                            pv.write(video_data)
                            pt.write(f"{count:4} {ftime} {fsize:7} {0:7}\n")
                            count += 1

        framesPerSecond = 30
        mp4File = playbackPath.with_suffix(".mp4")
        Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-r",
                f"{framesPerSecond}",
                "-i",
                f"{playbackPath}",
                "-vcodec",
                "copy",
                mp4File,
            ]
        )
        return {"response": "OK"}

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def recalc_start_stop_times(self):
        data = cherrypy.request.json

        # Create an observer object
        observer = ephem.Observer()

        # Set the observer's location (latitude and longitude)
        observer.lat = str(data["lat"])
        observer.lon = str(data["lon"])

        # Set the date to the current UTC time
        observer.date = ephem.now()

        # Create a Sun object
        sun = ephem.Sun()

        twilight = data["twilight"]

        sunset = observer.next_setting(sun)
        sunset_datetime = sunset.datetime()
        time_on = sunset_datetime + timedelta(minutes=twilight)

        sunrise = observer.next_rising(sun)
        sunrise_datetime = sunrise.datetime()
        time_off = sunrise_datetime - timedelta(minutes=twilight)

        self.startUTC = {"h": time_on.hour, "m": time_on.minute}
        self.stopUTC = {"h": time_off.hour, "m": time_off.minute}

        return {"response": "OK", "startUTC": self.startUTC, "stopUTC": self.stopUTC}

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def stars(self):
        starList = [
            "Sirius",
            "Canopus",
            "Rigil Kentaurus",
            "Arcturus",
            "Vega",
            "Capella",
            "Rigel",
            "Procyon",
            "Achernar",
            "Betelgeuse",
            "Hadar",
            "Altair",
            "Acrux",
            "Aldebaran",
            "Antares",
            "Spica",
            "Pollux",
            "Fomalhaut",
            "Deneb",
            "Mimosa",
            "Regulus",
            "Adhara",
            "Shaula",
            "Castor",
            "Gacrux",
        ]

        data = cherrypy.request.json
        observer = ephem.Observer()
        observer.lon = str(data["cameraLongitude"])
        observer.lat = str(data["cameraLatitude"])
        observer.elevation = data["cameraElevation"]

        if not data["path"]:
            return {"response": "OK", "sky_objects": []}

        path = os.path.basename(data["path"])

        year = int(path[1:5])
        month = int(path[5:7])
        day = int(path[7:9])
        hour = int(path[10:12])
        minute = int(path[12:14])
        second = int(path[14:16])

        result = []

        date = datetime(year, month, day, hour, minute, second)
        observer.date = date

        for name in starList:
            star = ephem.star(name)  # type: ignore[call-overload]
            star.compute(observer)
            if star.alt > 0.0:
                azim = degrees(star.az)
                elev = degrees(star.alt)
                result.append({"name": name, "azim": azim, "elev": elev})

        mars = ephem.Mars(date)
        mars.compute(observer)
        if mars.alt > 0.0:
            azim = degrees(mars.az)
            elev = degrees(mars.alt)
            result.append({"name": "Mars", "azim": azim, "elev": elev})

        venus = ephem.Venus(date)
        venus.compute(observer)
        if venus.alt > 0.0:
            azim = degrees(venus.az)
            elev = degrees(venus.alt)
            result.append({"name": "Venus", "azim": azim, "elev": elev})

        jupiter = ephem.Jupiter(date)
        jupiter.compute(observer)
        if jupiter.alt > 0.0:
            azim = degrees(jupiter.az)
            elev = degrees(jupiter.alt)
            result.append({"name": "Jupiter", "azim": azim, "elev": elev})

        saturn = ephem.Saturn(date)
        saturn.compute(observer)
        if saturn.alt > 0.0:
            azim = degrees(saturn.az)
            elev = degrees(saturn.alt)
            result.append({"name": "Saturn", "azim": azim, "elev": elev})

        moon = ephem.Moon(date)
        moon.compute(observer)
        if moon.alt > 0.0:
            azim = degrees(moon.az)
            elev = degrees(moon.alt)
            result.append({"name": "Moon", "azim": azim, "elev": elev})

        return {"response": "OK", "sky_objects": result}

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def new(self):
        logger.info("new")
        data = cherrypy.request.json
        self.relocateCurrent(data)
        events = self.getEvents("new")
        return events

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def saved(self):
        logger.info("Cmd: saved")
        data = cherrypy.request.json
        self.relocateCurrent(data)
        events = self.getEvents("saved")
        return events

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def trash(self):
        logger.info("Cmd: trash")
        data = cherrypy.request.json
        self.relocateCurrent(data)
        events = self.getEvents("trash")
        return events

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def calEvents(self):
        logger.info("Cmd: calEvents")
        pathDir = Path("calibration")
        paths = pathDir.glob("a*.jpg")
        events = [path.stem for path in paths]
        events.sort()
        return events

    @cherrypy.expose
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def get_running(self):
        logger.info("Cmd: get_running")
        result = {}
        result["response"] = state_to_str(shared_state_code.value)
        return result

    @cherrypy.expose
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def get_calibration(self):
        logger.info("Cmd: get_calibration")
        path = Path("calibration.json")
        if not path.exists():
            return asdict(Calibration())
        try:
            with path.open() as file:
                s = file.read()
                response = json.loads(s)
                return response
        except Exception:
            traceback.print_exc()
            return {}

    @cherrypy.expose
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def get_sky_objects(self):
        logger.info("Cmd: get_sky_objects")
        response = {}
        path = Path("sky_list.json")
        if path.exists():
            with open(path) as file:
                s = file.read()
                response = json.loads(s)
        return response

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def save_sky_objects(self):
        logger.info("Cmd: save_sky_objects")
        data = cherrypy.request.json
        sky_object_list = data["sky_object_list"]
        try:
            with open("sky_list.json", "w") as file:
                s = json.dumps(sky_object_list, sort_keys=True, indent=4)
                file.write(s)
        except Exception:
            traceback.print_exc()
        return {"response": "OK"}

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def do_calibration(self):
        logger.info("Cmd: do_calibration")
        data = cherrypy.request.json
        sky_object_list = data["sky_object_list"]
        calibration_state = data["calibration_state"]

        calVector = self.CalibrationVector(calibration_state)

        result = minimize(
            fun=self.TotalCalibrationError,
            x0=calVector,
            method="Nelder-Mead",
            args=(sky_object_list,),
            options={"maxiter": 10000, "disp": False},
        )
        logger.info(f"Iterations: {result.nfev} Error: {result.fun}")

        calObject = self.CalibrationObject(result["x"])
        self.CalibrationTruncate(calObject)

        calObject["cameraLatitude"] = calibration_state["cameraLatitude"]
        calObject["cameraLongitude"] = calibration_state["cameraLongitude"]
        calObject["cameraElevation"] = calibration_state["cameraElevation"]

        try:
            with open("calibration.json", "w") as file:
                s = json.dumps(calObject, sort_keys=True, indent=4)
                file.write(s)
        except Exception:
            traceback.print_exc()

        try:
            with open("sky_list.json", "w") as file:
                s = json.dumps(sky_object_list, sort_keys=True, indent=4)
                file.write(s)
        except Exception:
            traceback.print_exc()

        return {"response": "OK", "calibration_state": calObject}

    @cherrypy.expose
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def get_state(self):
        logger.info("Cmd: get_state")
        response = {}
        path = Path("state.json")
        if not path.exists():
            response = asdict(DetectorState())
        else:
            with path.open() as file:
                response = json.load(file)
                self.handle_set_state(response)
        response["frameRate"] = round(shared_frame_rate.value, 3)
        response["zenithAmplitude"] = round(shared_zenith_amplitude.value, 3)
        response["running"] = state_to_str(shared_state_code.value)
        response["numNew"] = len(glob("new/*.mp4"))
        response["numSaved"] = len(glob("saved/*.mp4"))
        response["numTrashed"] = len(glob("trash/*.mp4"))
        position = self.gpsModule.position()

        response["gpsLatitude"] = round(position[0], 4)
        response["gpsLongitude"] = round(position[1], 4)
        response["gpsTimeOffset"] = round(self.gpsModule.timeOffset(), 3)
        return response

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def set_state(self):
        logger.info("Cmd: set_state")
        data = cherrypy.request.json

        try:
            with open("state.json", "w") as file:
                self.handle_set_state(data)
                s = json.dumps(data, sort_keys=True, indent=4)
                file.write(s)
        except Exception:
            traceback.print_exc()
        return {"response": "OK"}

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def save_calibration(self):
        logger.info("Cmd: save_calibration")
        data = cherrypy.request.json

        try:
            with open("calibration.json", "w") as file:
                s = json.dumps(data, sort_keys=True, indent=4)
                file.write(s)
        except Exception:
            traceback.print_exc()
        return {"response": "OK"}

    @cherrypy.expose
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def empty_trash(self):
        logger.info("Cmd: empty_trash")
        try:
            for filename in os.listdir("trash"):
                file_path = os.path.join("trash", filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
            return {"response": "OK"}
        except OSError as e:
            return {"response": f"Error deleting files: {e}"}

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def compose(self):
        logger.info("Cmd: compose")
        data = cherrypy.request.json
        path = data["path"]
        Thread(target=self.composeEvent, daemon=True, args=(path,)).start()
        return {"response": "OK"}

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def analyze(self):
        logger.info("Cmd: analyze")
        data = cherrypy.request.json

        path = data["path"]
        Thread(target=self.analyzeEvent, daemon=True, args=(path,)).start()
        return {"response": "OK"}

    @cherrypy.expose
    @cherrypy.tools.json_in()  # type: ignore[attr-defined]
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def average(self):
        logger.info("Cmd: average")
        data = cherrypy.request.json
        utcTime = datetime.fromtimestamp(data["timestamp"] / 1000, tz=UTC)

        minute_string = utcTime.strftime("%Y%m%d_%H%M")

        Thread(target=self.averageEvent, daemon=True, args=(minute_string,)).start()
        return {"response": "OK"}

    @cherrypy.expose
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def toggle_camera(self):
        logger.info("Cmd: toggle_camera")
        if shared_state_code.value == StateCode.WATCHING:
            self.stop_camera_event.set()
        elif shared_state_code.value == StateCode.IDLE:
            Thread(target=self.runCamera, daemon=True).start()
        return {"response": "OK"}

    @cherrypy.expose
    @cherrypy.tools.json_out()  # type: ignore[attr-defined]
    def force_trigger(self):
        logger.info("Cmd: force_trigger")
        self.force_trigger_event.set()
        return {"response": "OK"}

    @cherrypy.expose
    def index(self):
        logger.info("Cmd: index")
        return open("public/index.html")


class GPS_Module:
    def __init__(self):
        self.gpsTimeOffset = 0.0
        self.gpsLatitude = 0.0
        self.gpsLongitude = 0.0
        self.count = 0

        parser = argparse.ArgumentParser(description="GPS Module arguments")
        parser.add_argument(
            "-p", "--port", type=str, default="/dev/ttyACM0", help="GPS Module port"
        )
        parser.add_argument(
            "-b", "--baudrate", type=int, default=9600, help="GPS Module baud rate"
        )

        args = parser.parse_args()
        self.port = args.port
        self.baudrate = args.baudrate

        self.thread = Thread(target=self.gpsThread, daemon=True).start()

    def position(self):
        return (round(self.gpsLatitude, 6), round(self.gpsLongitude, 6))

    def timeOffset(self):
        return round(self.gpsTimeOffset, 6)

    def decode(self, coord, direction):
        # Converts DDDMM.MMMMM > degrees
        s = coord.split(".")
        degrees = int(s[0][:-2])
        minutes = float(s[0][-2:] + "." + s[1])

        magnitude = degrees + minutes / 60.0
        if direction in ["W", "S"]:
            magnitude = -magnitude

        return round(magnitude, 6)

    def parseGPS(self, data):
        if data[0:6] != "$GPRMC":
            return

        now = datetime.now(UTC)
        sdata = data.split(",")
        if sdata[2] == "V":
            return False

        # print( "---Parsing GPRMC---", )
        hour = int(sdata[1][0:2])
        minute = int(sdata[1][2:4])
        second = int(sdata[1][4:6])
        hundredths = int(sdata[1][7:9])

        year = int(sdata[9][4:6]) + 2000
        month = int(sdata[9][2:4])
        day = int(sdata[9][0:2])

        measured = datetime(
            year, month, day, hour, minute, second, hundredths * 10000, tzinfo=UTC
        )
        difference = now - measured
        self.gpsTimeOffset = 0.9 * self.gpsTimeOffset + 0.1 * difference.total_seconds()

        # time = sdata[1][0:2] + ":" + sdata[1][2:4] + ":" + sdata[1][4:6]
        self.gpsLatitude = self.decode(sdata[3], sdata[4])  # latitude
        self.gpsLongitude = self.decode(sdata[5], sdata[6])  # longitute
        if self.count % 1800 == 0:
            msg = (
                f"GPS: lat={self.gpsLatitude:.4f}, lon={self.gpsLongitude:.4f}, "
                f"timeOffset={self.gpsTimeOffset:.3f}"
            )
            logger.info(msg)
        self.count += 1
        # speed = sdata[7]       #Speed in knots
        # trCourse = sdata[8]    #True course
        # date = sdata[9][0:2] + "/" + sdata[9][2:4] + "/" + sdata[9][4:6]#date
        return True

    def gpsThread(self):
        try:
            with serial.Serial(self.port, self.baudrate, timeout=1) as ser:
                while True:
                    line = ser.readline().decode("ascii", errors="replace").strip()
                    self.parseGPS(line)
        except serial.SerialException as e:
            logger.error(f"Could not open serial port {self.port}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in gpsThread: {e}")


conf = {
    "/": {
        "tools.staticdir.root": os.path.abspath(os.getcwd()),
        "tools.staticdir.on": True,
        "tools.staticdir.dir": "public",
    },
    "/new": {"tools.staticdir.on": True, "tools.staticdir.dir": "new"},
    "/saved": {"tools.staticdir.on": True, "tools.staticdir.dir": "saved"},
    "/trash": {"tools.staticdir.on": True, "tools.staticdir.dir": "trash"},
    "/calibration": {"tools.staticdir.on": True, "tools.staticdir.dir": "calibration"},
}


def startServer(shm):
    port = 9090
    server = SentinelServer(shm)
    background = Thread(target=server.backgroundProcess, daemon=True)
    background.start()

    cherrypy.config.update({"server.socket_port": port})
    cherrypy.config.update({"server.socket_host": "0.0.0.0"})
    cherrypy.quickstart(server, "/", conf)


def testCamera(shm):
    stop_camera = Event()
    force_trigger_event = Event()
    source = CameraSource(shm, stop_camera)
    detector = Detector(shm, force_trigger_event)
    p = Process(target=decoderProcess, args=(shm, source, detector))
    p.start()
    sleep(150)
    stop_camera.set()
    p.join()


def testComposer(shm):
    file_path = "new/s20260203_111235_707.mp4"
    with FileSource(shm, file_path) as source:
        composer = Composer("output.jpg")
        p = Process(target=decoderProcess, args=(shm, source, composer))
        p.start()
        p.join()


def testAnalyzer(shm):
    file_path = "new/s20260203_111235_707.mp4"
    with FileSource(shm, file_path) as source:
        analyzer = Analyzer("test.csv")
        p = Process(target=decoderProcess, args=(shm, source, analyzer))
        p.start()
        p.join()


def testGPS():
    server = GPS_Module()
    sleep(60)
    print(f"position: {server.position()}")
    print(f"timeOffset {server.timeOffset()}")


if __name__ == "__main__":
    format = "{time:YYYY-MM-DD HH:mm:ss} | {level} | line {line} | {message}"
    logger.remove()
    logger.add(sys.stderr, format=format)
    logger.add(
        "sentinel.log", format=format, rotation="12:00", retention=5, enqueue=True
    )

    logger.info("Program started")

    # Suppress the benign OSError(EBADF) that cheroot raises during garbage
    # collection when a client disconnects mid-request (e.g. during the
    # long-polling /subscribe endpoint).  Python calls sys.unraisablehook
    # for exceptions that occur inside __del__ and would otherwise be silently
    # discarded; we intercept only the specific case we know is harmless.
    _original_unraisablehook = sys.unraisablehook

    def _suppress_socket_cleanup_errors(unraisable):
        if (
            isinstance(unraisable.exc_value, OSError)
            and unraisable.exc_value.errno == errno.EBADF
            and getattr(unraisable.object, "__qualname__", "") == "IOBase.__del__"
        ):
            return  # cheroot socket wrapper closed after fd already gone
        _original_unraisablehook(unraisable)

    sys.unraisablehook = _suppress_socket_cleanup_errors

    # testGPS()
    # Switching to production disables autoreload and other dev tools
    cherrypy.config.update({"global": {"environment": "production"}})

    shm = SharedMemory(name=STORAGE_NAME, create=True, size=STORAGE_SIZE)
    try:
        # testCamera()
        # testAnalyzer()
        # testComposer()
        startServer(shm)
    except Exception as e:
        logger.error(f"error: {e}")
    finally:
        shm.close()
        shm.unlink()
