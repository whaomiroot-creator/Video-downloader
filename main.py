import os
import asyncio
import shutil
import re
import logging
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from urllib.parse import unquote

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from dotenv import load_dotenv
import yt_dlp

# ═══════════════════════════════════════════════════════════════
# ⚙️ CONFIGURAÇÃO E LOGGING
# ═══════════════════════════════════════════════════════════════

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
TEMP_DIR = Path(os.getenv("TEMP_DIR", "./temp"))
CLEANUP_INTERVAL_HOURS = int(os.getenv("CLEANUP_INTERVAL_HOURS", "1"))

DOWNLOAD_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.replace(' ', '_').strip()
    return name[:100] or "video_download"

# ═════════════════════════════════════════
# 🎯 MODELOS
# ═════════════════════════════════════════

class VideoFormat(BaseModel):
    format_id: str
    format_name: str
    resolution: Optional[str] = None
    codec: Optional[str] = None

class VideoMetadata(BaseModel):
    title: str
    duration_seconds: int
    thumbnail_url: str
    uploader: str
    formats: List[VideoFormat]

class VideoInfoRequest(BaseModel):
    url: HttpUrl

class VideoDownloadRequest(BaseModel):
    url: HttpUrl
    format_type: str = "mp4"
    quality: str = "best"

class DownloadResponse(BaseModel):
    status: str
    message: str
    download_url: Optional[str] = None

# ═══════════════════════════════════════════════════════════════
# 🔐 YT-DLP CONFIG
# ═══════════════════════════════════════════════════════════════

def get_ydl_opts(download_format: str = "mp4", quality: str = "best") -> Dict[str, Any]:
    out_tmpl = str(TEMP_DIR / f"dl_{uuid.uuid4().hex[:8]}_%(id)s.%(ext)s")
    
    quality_map = {
        "4k": "2160",
        "1080p": "1080",
        "720p": "720",
        "480p": "480",
        "360p": "360"
    }
    
    target_height = quality_map.get(quality)

    if download_format == "mp3":
        format_str = 'bestaudio/best'
    elif target_height:
        format_str = f'bestvideo[height<={target_height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={target_height}]/best'
    else:
        format_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    opts = {
        'outtmpl': out_tmpl,
        'format': format_str,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'referer': 'https://www.google.com/',
        'logger': logger,
    }

    if download_format == "mp3":
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        opts['postprocessors'] = [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }]
    
    return opts

# ═══════════════════════════════════════════════════════════════
# 🎬 PROCESSAMENTO
# ═══════════════════════════════════════════════════════════════

def extract_video_info(url: str) -> VideoMetadata:
    with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            formats = []
            raw_formats = info.get('formats', [])
            for f in raw_formats[:15]:
                if f.get('vcodec') != 'none':
                    formats.append(VideoFormat(
                        format_id=str(f.get('format_id')),
                        format_name=str(f.get('format')),
                        resolution=str(f.get('height', 'auto')),
                        codec=str(f.get('vcodec'))
                    ))
            
            return VideoMetadata(
                title=info.get('title', 'Video'),
                duration_seconds=int(info.get('duration') or 0),
                thumbnail_url=info.get('thumbnail', ''),
                uploader=info.get('uploader', 'Unknown'),
                formats=formats
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erro ao obter info: {str(e)}")

def download_video_logic(url: str, format_type: str, quality: str) -> tuple[Path, str]:
    opts = get_ydl_opts(format_type, quality)
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
            original_title = info.get('title', 'video')
            expected_filename = ydl.prepare_filename(info)
            
            base_path = os.path.splitext(expected_filename)[0]
            final_ext = ".mp3" if format_type == "mp3" else ".mp4"
            final_path = Path(base_path + final_ext)

            if not final_path.exists():
                for f in TEMP_DIR.glob(f"{Path(base_path).name}*"):
                    if f.suffix == final_ext:
                        return f, original_title
                raise Exception("Arquivo não encontrado após processamento.")
                
            return final_path, original_title
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erro no download: {str(e)}")

def cleanup_old_files():
    cutoff = datetime.now() - timedelta(hours=CLEANUP_INTERVAL_HOURS)
    for d in [TEMP_DIR, DOWNLOAD_DIR]:
        for f in d.glob("*"):
            if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                try: f.unlink()
                except: pass

# ═══════════════════════════════════════════════════════════════
# 🔌 API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="SaaS Video Downloader Universal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rota para servir o Frontend (Resolve o erro 404)
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = Path("index.html")
    if index_path.exists():
        return index_path.read_text(encoding="utf-8")
    return "<h1>Erro: index.html não encontrado!</h1>"

@app.get("/api/health")
async def health():
    return {"status": "online", "ffmpeg": shutil.which("ffmpeg") is not None}

@app.post("/api/info", response_model=VideoMetadata)
async def get_info(request: VideoInfoRequest):
    return extract_video_info(str(request.url))

@app.post("/api/download", response_model=DownloadResponse)
async def download(request: VideoDownloadRequest, bg: BackgroundTasks):
    try:
        temp_file, original_title = download_video_logic(str(request.url), request.format_type, request.quality)
        
        safe_id = uuid.uuid4().hex[:6]
        timestamp = datetime.now().strftime("%H%M%S")
        safe_filename = f"dl_{timestamp}_{safe_id}{temp_file.suffix}"
        
        final_path = DOWNLOAD_DIR / safe_filename
        shutil.move(str(temp_file), str(final_path))
        
        bg.add_task(cleanup_old_files)
        
        return DownloadResponse(
            status="success",
            message="Download concluído",
            download_url=f"/api/file/{safe_filename}?title={sanitize_filename(original_title)}"
        )
    except Exception as e:
        logger.error(f"Erro fatal: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/file/{filename}")
async def get_file(filename: str, title: Optional[str] = "video"):
    p = DOWNLOAD_DIR / filename
    if not p.exists():
        raise HTTPException(404, "Arquivo sumiu!")
    return FileResponse(p, media_type="application/octet-stream", filename=f"{title}{p.suffix}")

# Monta arquivos estáticos caso você tenha pastas CSS/JS
if Path("static").exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    # Importante: se rodar via main.py, ele usa a porta 8000
    uvicorn.run(app, host="127.0.0.1", port=8000)