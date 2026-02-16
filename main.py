import os
import asyncio
import shutil
import re
import logging
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from dotenv import load_dotenv
import yt_dlp

# CONFIGURAÇÃO
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

# OPÇÕES DE EVASÃO (O segredo para o Render não ser bloqueado)
def get_base_ydl_opts() -> Dict[str, Any]:
    return {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'no_color': True,
        # Força o uso de formatos que o YouTube libera mais fácil para servidores
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios'],
                'skip': ['dash', 'hls']
            }
        },
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    }

def get_download_opts(format_type: str, quality: str) -> Dict[str, Any]:
    opts = get_base_ydl_opts()
    unique_id = uuid.uuid4().hex[:8]
    opts['outtmpl'] = str(TEMP_DIR / f"dl_{unique_id}_%(id)s.%(ext)s")
    
    if format_type == "mp3":
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
    else:
        # Tenta baixar a melhor qualidade disponível que seja compatível
        opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]
    
    return opts

# API MODELS
class VideoInfoRequest(BaseModel):
    url: HttpUrl

class VideoDownloadRequest(BaseModel):
    url: HttpUrl
    format_type: str = "mp4"
    quality: str = "best"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    path = Path("index.html")
    return path.read_text(encoding="utf-8") if path.exists() else "API Online"

@app.get("/api/health")
async def health():
    return {"status": "online", "ffmpeg": shutil.which("ffmpeg") is not None}

@app.post("/api/info")
async def get_info(request: VideoInfoRequest):
    # Tenta extrair info com várias tentativas se necessário
    with yt_dlp.YoutubeDL(get_base_ydl_opts()) as ydl:
        try:
            info = ydl.extract_info(str(request.url), download=False)
            return {
                "title": info.get('title', 'Video'),
                "duration_seconds": int(info.get('duration') or 0),
                "thumbnail_url": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'Unknown'),
                "formats": []
            }
        except Exception as e:
            logger.error(f"Erro YT-DLP: {e}")
            raise HTTPException(status_code=400, detail="O YouTube bloqueou o acesso deste servidor. Tente outro link.")

@app.post("/api/download")
async def download(request: VideoDownloadRequest, bg: BackgroundTasks):
    opts = get_download_opts(request.format_type, request.quality)
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(str(request.url), download=True)
            temp_file = Path(ydl.prepare_filename(info))
            
            # Ajuste de extensão para MP3
            if request.format_type == "mp3":
                temp_file = temp_file.with_suffix(".mp3")

            final_name = f"final_{uuid.uuid4().hex[:6]}{temp_file.suffix}"
            final_path = DOWNLOAD_DIR / final_name
            
            # No Render, precisamos garantir que o arquivo existe antes de mover
            if temp_file.exists():
                shutil.move(str(temp_file), str(final_path))
            else:
                # Fallback: procura o arquivo mais recente no temp
                files = sorted(TEMP_DIR.glob("*"), key=os.path.getmtime, reverse=True)
                if files: shutil.move(str(files[0]), str(final_path))
                else: raise Exception("Arquivo não gerado.")

            return {
                "status": "success",
                "download_url": f"/api/file/{final_name}?title={sanitize_filename(info.get('title','video'))}"
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/file/{filename}")
async def get_file(filename: str, title: Optional[str] = "video"):
    p = DOWNLOAD_DIR / filename
    if not p.exists(): raise HTTPException(404, "Arquivo expirou.")
    return FileResponse(p, media_type="application/octet-stream", filename=f"{title}{p.suffix}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
