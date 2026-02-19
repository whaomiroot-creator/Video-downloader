import os
import asyncio
import shutil
import re
import logging
import uuid
import time
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import yt_dlp

# --- CONFIGURAÇÃO ---
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
TEMP_DIR = BASE_DIR / "temp"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

progress_db: Dict[str, float] = {}
results_db: Dict[str, str] = {}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- SISTEMA DE LIMPEZA AUTOMÁTICA ---
async def auto_cleaner():
    """Remove arquivos com mais de 30 minutos para economizar espaço no Render"""
    while True:
        try:
            current_time = time.time()
            for folder in [DOWNLOAD_DIR, TEMP_DIR]:
                for file_path in folder.glob("*"):
                    if file_path.is_file() and (current_time - file_path.stat().st_mtime) > 1800:
                        file_path.unlink()
                        logger.info(f"Auto-Cleaner: Removido {file_path.name}")
        except Exception as e:
            logger.error(f"Erro no Auto-Cleaner: {e}")
        
        await asyncio.sleep(600) 

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_cleaner())

# --- FUNÇÕES DE SUPORTE ---
def progress_hook(d):
    download_id = d.get('info_dict', {}).get('v_engine_id')
    if not download_id: return
    if d['status'] == 'downloading':
        try:
            p = d.get('_percent_str', '0%')
            p_clean = re.sub(r'\x1b\[[0-9;]*m', '', p).replace('%', '').strip()
            progress_db[download_id] = float(p_clean)
        except: pass
    elif d['status'] == 'finished':
        progress_db[download_id] = 100.0

def task_download_video(download_id: str, url: str, format_type: str):
    base_name = f"v_{download_id}"
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'progress_hooks': [progress_hook],
        'v_engine_id': download_id,
        'outtmpl': str(TEMP_DIR / f"{base_name}_%(id)s.%(ext)s")
    }
    
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"

    if format_type == "mp3":
        opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        })
    else:
        opts['format'] = 'best'

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info['v_engine_id'] = download_id
            ydl.process_info(info)
            
            for _ in range(10):
                files = list(TEMP_DIR.glob(f"{base_name}*"))
                media_files = [f for f in files if f.suffix.lower() in ['.mp3', '.mp4', '.webm', '.mkv']]
                if media_files:
                    temp_file = media_files[0]
                    final_name = f"final_{download_id}{temp_file.suffix}"
                    shutil.move(str(temp_file), str(DOWNLOAD_DIR / final_name))
                    title = re.sub(r'[^\w\s-]', '', info.get('title', 'video'))
                    results_db[download_id] = f"/api/file/{final_name}?title={title}"
                    return
                time.sleep(2)
    except Exception as e:
        logger.error(f"Erro na Task {download_id}: {e}")
        progress_db[download_id] = -1

# --- ROTAS DA API ---
@app.get("/")
async def serve_index():
    index_path = BASE_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return {"message": "API rodando, mas index.html não foi encontrado."}

@app.get("/api/progress/{download_id}")
async def get_progress(download_id: str):
    return {
        "progress": progress_db.get(download_id, 0),
        "download_url": results_db.get(download_id, None)
    }

class DownloadRequest(BaseModel):
    url: str
    format_type: str = "mp4"

@app.post("/api/download")
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    u_id = uuid.uuid4().hex[:8]
    progress_db[u_id] = 0.1
    background_tasks.add_task(task_download_video, u_id, request.url, request.format_type)
    return {"download_id": u_id}

@app.get("/api/file/{filename}")
async def get_file(filename: str, title: str = "video"):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    return FileResponse(file_path, filename=f"{title}{file_path.suffix}")

@app.post("/api/info")
async def get_info(request: DownloadRequest):
    opts = {'quiet': True, 'no_warnings': True}
    if os.path.exists("cookies.txt"): opts['cookiefile'] = "cookies.txt"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Extração de dados com segurança
            info = ydl.extract_info(request.url, download=False)
            
            # Pega a duração em segundos (ou 0 se não encontrar)
            duration_seconds = info.get('duration', 0)
            
            return {
                "title": info.get('title', 'Video'),
                "thumbnail_url": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'User'),
                "duration": int(duration_seconds) if duration_seconds else 0 # <- NOVO
            }
    except Exception as e:
        logger.error(f"Erro ao buscar info: {e}")
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
