import os
import asyncio
import shutil
import re
import logging
import uuid
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks
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

# Banco de dados de progresso e resultados finais
progress_db: Dict[str, float] = {}
results_db: Dict[str, str] = {} # Guarda o link final quando pronto

def progress_hook(d):
    # Pega o ID que injetamos manualmente
    download_id = d.get('info_dict', {}).get('v_engine_id')
    if not download_id:
        return

    if d['status'] == 'downloading':
        try:
            p = d.get('_percent_str', '0%')
            p_clean = re.sub(r'\x1b\[[0-9;]*m', '', p).replace('%', '').strip()
            progress_db[download_id] = float(p_clean)
        except:
            pass
    elif d['status'] == 'finished':
        progress_db[download_id] = 100.0

def get_base_ydl_opts(download_id: str) -> Dict[str, Any]:
    return {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'progress_hooks': [progress_hook],
        # Injetamos o ID aqui para o hook encontrar depois
        'v_engine_id': download_id 
    }

class VideoRequest(BaseModel):
    url: str
    format_type: str = "mp4"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- LÓGICA DE DOWNLOAD EM SEGUNDO PLANO ---
def task_download_video(download_id: str, url: str, format_type: str):
    base_name = f"v_{download_id}"
    opts = get_base_ydl_opts(download_id)
    opts['outtmpl'] = str(TEMP_DIR / f"{base_name}_%(id)s.%(ext)s")
    
    if format_type == "mp3":
        opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        })
    else:
        opts['format'] = 'best'

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Forçamos o ID dentro do info_dict antes do download
            info = ydl.extract_info(url, download=False)
            info['v_engine_id'] = download_id
            ydl.process_info(info) # Inicia o download real
            
            # Localização do arquivo
            time_out = 0
            while time_out < 5: # Espera o arquivo aparecer no disco
                files = list(TEMP_DIR.glob(f"{base_name}*"))
                media_files = [f for f in files if f.suffix.lower() in ['.mp3', '.mp4', '.webm', '.mkv']]
                if media_files:
                    temp_file = media_files[0]
                    final_name = f"final_{download_id}{temp_file.suffix}"
                    shutil.move(str(temp_file), str(DOWNLOAD_DIR / final_name))
                    
                    # Salva o link para o front coletar
                    title = re.sub(r'[^\w\s-]', '', info.get('title', 'video'))
                    results_db[download_id] = f"/api/file/{final_name}?title={title}"
                    break
                time.sleep(1)
                time_out += 1
    except Exception as e:
        logger.error(f"Erro na Task: {e}")
        progress_db[download_id] = -1 # Sinal de erro

# --- ROTAS ---
@app.get("/api/progress/{download_id}")
async def get_progress(download_id: str):
    return {
        "progress": progress_db.get(download_id, 0),
        "download_url": results_db.get(download_id, None)
    }

@app.post("/api/download")
async def start_download(request: VideoRequest, background_tasks: BackgroundTasks):
    unique_id = uuid.uuid4().hex[:8]
    progress_db[unique_id] = 0.1
    
    # Adiciona a tarefa para rodar "atrás das cortinas"
    background_tasks.add_task(task_download_video, unique_id, request.url, request.format_type)
    
    # Responde NA HORA para o frontend com o ID correto
    return {"status": "started", "download_id": unique_id}

@app.get("/api/file/{filename}")
async def get_file(filename: str, title: str = "video"):
    p = DOWNLOAD_DIR / filename
    if not p.exists(): raise HTTPException(404)
    return FileResponse(p, filename=f"{title}{p.suffix}")

@app.post("/api/info")
async def get_info(request: VideoRequest):
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        info = ydl.extract_info(request.url, download=False)
        return {
            "title": info.get('title', 'Video'),
            "thumbnail_url": info.get('thumbnail', ''),
            "uploader": info.get('uploader', 'User')
        }

if __name__ == "__main__":
    import uvicorn
    import time
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
