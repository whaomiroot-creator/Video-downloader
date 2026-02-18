import os
import asyncio
import shutil
import re
import logging
import uuid
import time
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import yt_dlp

# --- CONFIGURAÇÃO ---
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path("downloads")
TEMP_DIR = Path("temp")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

progress_db: Dict[str, float] = {}

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name.replace(' ', '_').strip()[:100] or "video"

def progress_hook(d):
    download_id = d.get('info_dict', {}).get('v_engine_id')
    if d['status'] == 'downloading':
        try:
            p = d.get('_percent_str', '0%')
            # Limpa caracteres de cor e extrai número
            p_clean = re.sub(r'\x1b\[[0-9;]*m', '', p).replace('%', '').strip()
            if download_id:
                progress_db[download_id] = float(p_clean)
                logger.info(f"Progresso [{download_id}]: {p_clean}%")
        except:
            pass
    elif d['status'] == 'finished':
        if download_id:
            progress_db[download_id] = 100.0

def get_base_ydl_opts() -> Dict[str, Any]:
    opts = {
        'quiet': False, # Deixamos False para ver erros no log do Render
        'no_warnings': False,
        'nocheckcertificate': True,
        'no_color': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'progress_hooks': [progress_hook],
    }
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"
        logger.info("🍪 Cookies carregados.")
    return opts

# --- MODELOS ---
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

# --- ROTAS ---
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    path = Path("index.html")
    return path.read_text(encoding="utf-8") if path.exists() else "API Online"

@app.get("/api/progress/{download_id}")
async def get_progress(download_id: str):
    return {"progress": progress_db.get(download_id, 0)}

@app.post("/api/info")
async def get_info(request: VideoInfoRequest):
    with yt_dlp.YoutubeDL(get_base_ydl_opts()) as ydl:
        try:
            info = ydl.extract_info(request.url.strip(), download=False)
            return {
                "title": info.get('title', 'Video Pinterest'),
                "duration_seconds": int(info.get('duration') or 0),
                "thumbnail_url": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'User')
            }
        except Exception as e:
            logger.error(f"Erro Info: {e}")
            raise HTTPException(status_code=400, detail="Vídeo indisponível ou link inválido.")

@app.post("/api/download")
async def download(request: VideoDownloadRequest):
    unique_id = uuid.uuid4().hex[:8]
    progress_db[unique_id] = 0.1 # Inicia com 0.1 para o front saber que começou
    
    base_name = f"v_{unique_id}"
    opts = get_base_ydl_opts()
    opts['outtmpl'] = str(TEMP_DIR / f"{base_name}_%(id)s.%(ext)s")

    # Simplificação total dos formatos para evitar erro de "Requested format not available"
    if request.format_type == "mp3":
        opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        })
    else:
        # Pega o melhor vídeo que já venha com áudio (evita merge complexo que trava em 0%)
        opts['format'] = 'best' 

    def run_dl():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.params['v_engine_id'] = unique_id
            return ydl.extract_info(request.url.strip(), download=True)

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, run_dl)
        
        # Busca o arquivo
        await asyncio.sleep(1)
        files = list(TEMP_DIR.glob(f"{base_name}*"))
        media_files = [f for f in files if f.suffix.lower() in ['.mp3', '.mp4', '.webm', '.mkv', '.mov']]

        if not media_files:
            raise Exception("O download falhou em gerar um arquivo de mídia.")

        temp_file = media_files[0]
        final_ext = ".mp3" if request.format_type == "mp3" else temp_file.suffix
        final_name = f"final_{unique_id}{final_ext}"
        final_path = DOWNLOAD_DIR / final_name
        
        shutil.move(str(temp_file), str(final_path))
        
        return {
            "status": "success",
            "download_id": unique_id,
            "download_url": f"/api/file/{final_name}?title={sanitize_filename(info.get('title','video'))}"
        }
    except Exception as e:
        logger.error(f"Erro Crítico: {e}")
        progress_db[unique_id] = 0 # Reseta progresso em caso de erro
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/file/{filename}")
async def get_file(filename: str, title: Optional[str] = "video"):
    p = DOWNLOAD_DIR / filename
    if not p.exists(): raise HTTPException(404, "Arquivo não encontrado.")
    return FileResponse(p, media_type="application/octet-stream", filename=f"{title}{p.suffix}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
