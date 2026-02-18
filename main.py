import os
import asyncio
import shutil
import re
import logging
import uuid
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import yt_dlp

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path("downloads")
TEMP_DIR = Path("temp")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name.replace(' ', '_').strip()[:100] or "video"

def get_base_ydl_opts() -> Dict[str, Any]:
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    }
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"
        logger.info("🍪 SUCESSO: Cookies carregados!")
    return opts

class VideoInfoRequest(BaseModel):
    url: str

class VideoDownloadRequest(BaseModel):
    url: str
    format_type: str = "mp4"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    p = Path("index.html")
    return p.read_text(encoding="utf-8") if p.exists() else "API Online"

# ROTA CORRIGIDA PARA EVITAR ERRO 400
@app.post("/api/info")
async def get_info(request: VideoInfoRequest):
    opts = get_base_ydl_opts()
    # 'noplaylist' e 'extract_flat' garantem que ele não se perca em formatos complexos
    opts.update({'extract_flat': True, 'force_generic_extractor': False})
    
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            # Pegamos apenas o básico sem processar formatos (evita o erro do log)
            info = ydl.extract_info(request.url.strip(), download=False)
            return {
                "title": info.get('title', 'Vídeo'),
                "duration_seconds": int(info.get('duration') or 0),
                "thumbnail_url": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'Canal'),
                "formats": [] 
            }
        except Exception as e:
            logger.error(f"Erro YT-DLP: {e}")
            raise HTTPException(status_code=400, detail="Erro de comunicação com o YouTube. Tente novamente.")

@app.post("/api/download")
async def download(request: VideoDownloadRequest, bg: BackgroundTasks):
    unique_id = uuid.uuid4().hex[:8]
    opts = get_base_ydl_opts()
    opts['outtmpl'] = str(TEMP_DIR / f"dl_{unique_id}_%(id)s.%(ext)s")
    
    if request.format_type == "mp3":
        opts.update({'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}]})
    else:
        opts.update({'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'})

    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(request.url.strip(), download=True)
            temp_file = Path(ydl.prepare_filename(info))
            if request.format_type == "mp3": temp_file = temp_file.with_suffix(".mp3")
            
            final_name = f"v_{uuid.uuid4().hex[:5]}{temp_file.suffix}"
            final_path = DOWNLOAD_DIR / final_name
            shutil.move(str(temp_file), str(final_path))

            return {"status": "success", "download_url": f"/api/file/{final_name}?title={sanitize_filename(info.get('title','video'))}"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/file/{filename}")
async def get_file(filename: str, title: Optional[str] = "video"):
    p = DOWNLOAD_DIR / filename
    if not p.exists(): raise HTTPException(404)
    return FileResponse(p, media_type="application/octet-stream", filename=f"{title}{p.suffix}")
