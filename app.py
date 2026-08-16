import asyncio
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

import imageio_ffmpeg
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

APP_NAME = "FAZ Audio Worker"
WORKER_API_KEY = os.environ.get