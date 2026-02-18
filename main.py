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

# CONFIGURAÇÃO DE EVASÃO MÁXIMA
def get_base_ydl_opts() -> Dict[str, Any]:
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'no_color': True,
        # Força o uso de IPv4, já que IPv6 em datacenters é bloqueado na hora
        'source_address': '0.0.0.0',
        'socket_timeout': 30,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    }
    
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"
        logger.info("🍪 COOKIES CARREGADOS")
    
    return opts

class VideoInfoRequest(BaseModel):
    url: str

class VideoDownloadRequest(BaseModel):
    url: str
    format_type: str = "mp4"

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
    return path.read_text(encoding="utf-8") if path.exists() else "V-ENGINE ONLINE"

@app.post("/api/info")
async def get_info(request: VideoInfoRequest):
    opts = get_base_ydl_opts()
    # TRUQUE FINAL: 'extract_flat' e desativar busca de formatos complexos
    opts.update({
        'extract_flat': 'in_playlist',
        'force_generic_extractor': False,
    })
    
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            url = request.url.strip()
            # Pede apenas os metadados básicos sem processar o vídeo
            info = ydl.extract_info(url, download=False, process=False)
            
            return {
                "title": info.get('title', 'Vídeo'),
                "duration_seconds": int(info.get('duration') or 0),
                "thumbnail_url": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'Canal'),
                "formats": []
            }
        except Exception as e:
            logger.error(f"ERRO INFO: {e}")
            raise HTTPException(status_code=400, detail="O YouTube bloqueou a requisição. Tente novamente.")

@app.post("/api/download")
async def download(request: VideoDownloadRequest, bg: BackgroundTasks):
    unique_id = uuid.uuid4().hex[:8]
    opts = get_base_ydl_opts()
    opts['outtmpl'] = str(TEMP_DIR / f"dl_{unique_id}_%(id)s.%(ext)s")
    
    if request.format_type == "mp3":
        opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
        })
    else:
        opts.update({
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        })

    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(request.url.strip(), download=True)
            temp_file = Path(ydl.prepare_filename(info))
            
            if request.format_type == "mp3":
                temp_file = temp_file.with_suffix(".mp3")
            
            final_name = f"v_{uuid.uuid4().hex[:6]}{temp_file.suffix}"
            final_path = DOWNLOAD_DIR / final_name
            
            if temp_file.exists():
                shutil.move(str(temp_file), str(final_path))
                return {
                    "status": "success",
                    "download_url": f"/api/file/{final_name}?title={sanitize_filename(info.get('title','video'))}"
                }
            raise Exception("Erro ao localizar arquivo.")
        except Exception as e:
            logger.error(f"ERRO DOWNLOAD: {e}")
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/file/{filename}")
async def get_file(filename: str, title: Optional[str] = "video"):
    p = DOWNLOAD_DIR / filename
    if not p.exists(): raise HTTPException(404)
    return FileResponse(p, media_type="application/octet-stream", filename=f"{title}{p.suffix}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
