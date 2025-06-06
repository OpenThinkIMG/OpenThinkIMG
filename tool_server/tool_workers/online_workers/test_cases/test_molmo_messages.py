"""Send a test message."""
import argparse
import json
import time
from io import BytesIO
import cv2
import sys
import numpy as np


import requests
import base64

import torch
import torchvision.transforms.functional as F

import uuid
import os
import re
import io

from PIL import Image, ImageDraw
from tool_server.utils.utils import *
from tool_server.utils.server_utils import *
import matplotlib.pyplot as plt




def load_image(image_path):
    img = Image.open(image_path).convert('RGB')
    # import ipdb; ipdb.set_trace()
    # resize if needed
    w, h = img.size
    if max(h, w) > 800:
        if h > w:
            new_h = 800
            new_w = int(w * 800 / h)
        else:
            new_w = 800
            new_h = int(h * 800 / w)
        # import ipdb; ipdb.set_trace()
        img = F.resize(img, (new_h, new_w))
    return img

def encode(image: Image):
    buffered = BytesIO()
    image.save(buffered, format="JPEG")
    img_b64_str = base64.b64encode(buffered.getvalue()).decode()
    return img_b64_str


def main():
    model_name = args.model_name

    if args.worker_address:
        worker_addr = args.worker_address
    else:
        controller_addr = args.controller_address
        # ret = requests.post(controller_addr + "/refresh_all_workers")
        ret = requests.post(controller_addr + "/list_models")
        print(f"list_models: {ret.json()}")
        models = ret.json()["models"]
        models.sort()
        print(f"Models: {models}")

        ret = requests.post(
            controller_addr + "/get_worker_address", json={"model": model_name}
        )
        worker_addr = ret.json()["address"]
        print(f"worker_addr: {worker_addr}")

    if worker_addr == "":
        print(f"No available workers for {model_name}")
        return

    headers = {"User-Agent": "FastChat Client"}
    if args.send_image:
        img = load_image(args.image_path)
        img_arg = encode(img)
    else:
        img_arg = args.image_path
    datas = {
        "model": model_name,
        "param": args.obj,
        "image": img_arg,
    }
    tic = time.time()
    # breakpoint()
    response = requests.post(
        worker_addr + "/worker_generate",
        headers=headers,
        json=datas,
    )
    toc = time.time()
    print(f"Time: {toc - tic:.3f}s")

    print("detection result:")
    print(response)
    print(response.json())
    # response is 'Response' with :
    # ['_content', '_content_consumed', '_next', 'status_code', 'headers', 'raw', 'url', 'encoding', 'history', 'reason', 'cookies', 'elapsed', 'request', 'connection', '__module__', '__doc__', '__attrs__', '__init__', '__enter__', '__exit__', '__getstate__', '__setstate__', '__repr__', '__bool__', '__nonzero__', '__iter__', 'ok', 'is_redirect', 'is_permanent_redirect', 'next', 'apparent_encoding', 'iter_content', 'iter_lines', 'content', 'text', 'json', 'links', 'raise_for_status', 'close', '__dict__', '__weakref__', '__hash__', '__str__', '__getattribute__', '__setattr__', '__delattr__', '__lt__', '__le__', '__eq__', '__ne__', '__gt__', '__ge__', '__new__', '__reduce_ex__', '__reduce__', '__subclasshook__', '__init_subclass__', '__format__', '__sizeof__', '__dir__', '__class__']

    # visualize
    res = response.json()
    print(f"molmo response: {res['text']}")
    image_base64 = res["edited_image"]
    image = base64_to_pil(image_base64)
    image.save("mathvista_pointed.jpg")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # worker parameters
    parser.add_argument(
        "--controller-address", type=str, default="http://localhost:20001"
    )
    parser.add_argument("--worker-address", type=str)
    parser.add_argument("--model-name", type=str, default='Point')

    # model parameters
    parser.add_argument(
        "--obj", type=str, default="Point E"
    )
    parser.add_argument(
        "--send_image", action="store_true",
    )
    parser.add_argument(
        "--image_path", type=str, default="./mathvista_35.jpg"
    )
    args = parser.parse_args()
    args.send_image = True

    main()
