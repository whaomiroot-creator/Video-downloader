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
from pydantic import BaseModel
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

DOWNLOAD_DIR = Path("downloads")
TEMP_DIR = Path("temp")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.replace(' ', '_').strip()
    return name[:100] or "video_download"

# ═══════════════════════════════════════════════════════════════
# 🔐 YT-DLP CONFIG (COM SUPORTE A COOKIES E EVASÃO)
# ═══════════════════════════════════════════════════════════════

def get_base_ydl_opts() -> Dict[str, Any]:
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'no_color': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'skip': ['dash', 'hls']
            }
        },
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    }
    
    # Carregamento automático do Secret File do Render
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"
        logger.info("🍪 SUCESSO: Arquivo cookies.txt carregado!")
    else:
        logger.warning("⚠️ AVISO: cookies.txt não encontrado. Erros de BOT podem ocorrer.")
        
    return opts

# ═════════════════════════════════════════
# 🎯 MODELOS (PYDANTIC V1 COMPATÍVEL)
# ═════════════════════════════════════════

class VideoInfoRequest(BaseModel):
    url: str

class VideoDownloadRequest(BaseModel):
    url: str
    format_type: str = "mp4"
    quality: str = "best"

# ═══════════════════════════════════════════════════════════════
# 🔌 API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="V-ENGINE API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = Path("index.html")
    if index_path.exists():
        return index_path.read_text(encoding="utf-8")
    return "<h1>API V-ENGINE Online</h1>"

@app.get("/api/health")
async def health():
    return {"status": "online", "ffmpeg": shutil.which("ffmpeg") is not None}

@app.post("/api/info")
async def get_info(request: VideoInfoRequest):
    # Opções simplificadas para evitar o erro "Requested format is not available"
    opts = get_base_ydl_opts()
    opts['format'] = 'best/bestvideo+bestaudio' 
    
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            url = request.url.strip()
            # process=True é necessário para pegar metadados, mas simplificamos o formato acima
            info = ydl.extract_info(url, download=False, process=True)
            
            return {
                "title": info.get('title', 'Video'),
                "duration_seconds": int(info.get('duration') or 0),
                "thumbnail_url": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'Unknown'),
                "formats": [] # Omitimos para evitar erros de processamento no front
            }
        except Exception as e:
            logger.error(f"Erro YT-DLP Info: {e}")
            raise HTTPException(status_code=400, detail="Vídeo indisponível ou bloqueado. Verifique os cookies.")

@app.post("/api/download")
async def download(request: VideoDownloadRequest, bg: BackgroundTasks):
    unique_id = uuid.uuid4().hex[:8]
    opts = get_base_ydl_opts()
    opts['outtmpl'] = str(TEMP_DIR / f"dl_{unique_id}_%(id)s.%(ext)s")
    
    # Configuração de Formato de Download
    if request.format_type == "mp3":
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        # Pega o melhor MP4 ou converte se necessário
        opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        opts['postprocessors'] = [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }]

    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(request.url.strip(), download=True)
            temp_file = Path(ydl.prepare_filename(info))
            
            # Correção de extensão após post-processor
            if request.format_type == "mp3":
                temp_file = temp_file.with_suffix(".mp3")
            elif not temp_file.exists() and temp_file.with_suffix(".mp4").exists():
                temp_file = temp_file.with_suffix(".mp4")

            final_name = f"vengine_{uuid.uuid4().hex[:6]}{temp_file.suffix}"
            final_path = DOWNLOAD_DIR / final_name
            
            # Pequeno delay para garantir que o sistema de arquivos liberou o arquivo
            await asyncio.sleep(1)
            
            if temp_file.exists():
                shutil.move(str(temp_file), str(final_path))
            else:
                # Busca desesperada: se o yt-dlp mudou o nome mas o ID bate
                possiveis = list(TEMP_DIR.glob(f"dl_{unique_id}*"))
                if possiveis:
                    shutil.move(str(possiveis[0]), str(final_path))
                else:
                    raise Exception("Arquivo não encontrado após download.")

            return {
                "status": "success",
                "download_url": f"/api/file/{final_name}?title={sanitize_filename(info.get('title','video'))}"
            }
        except Exception as e:
            logger.error(f"Erro YT-DLP Download: {e}")
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/file/{filename}")
async def get_file(filename: str, title: Optional[str] = "video"):
    p = DOWNLOAD_DIR / filename
    if not p.exists():
        raise HTTPException(404, "O arquivo expirou ou não foi encontrado.")
    return FileResponse(p, media_type="application/octet-stream", filename=f"{title}{p.suffix}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
