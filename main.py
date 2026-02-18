import os
import asyncio
import shutil
import re
import logging
import uuid
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
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

# Banco de dados temporário para o progresso
progress_db: Dict[str, float] = {}

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name.replace(' ', '_').strip()[:100] or "video"

# Hook para capturar o progresso do yt-dlp
def progress_hook(d):
    if d['status'] == 'downloading':
        try:
            p = d.get('_percent_str', '0%')
            p = re.sub(r'\x1b\[[0-9;]*m', '', p).replace('%', '').strip()
            download_id = d.get('info_dict', {}).get('v_engine_id')
            if download_id:
                progress_db[download_id] = float(p)
        except:
            pass
    elif d['status'] == 'finished':
        download_id = d.get('info_dict', {}).get('v_engine_id')
        if download_id:
            progress_db[download_id] = 100.0

# CONFIGURAÇÃO BASE
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
        'progress_hooks': [progress_hook],
    }
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"
    return opts

# MODELOS
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
    return path.read_text(encoding="utf-8") if path.exists() else "API Online"

@app.get("/api/health")
async def health():
    return {"status": "online", "ffmpeg": shutil.which("ffmpeg") is not None}

@app.get("/api/progress/{download_id}")
async def get_progress(download_id: str):
    return {"progress": progress_db.get(download_id, 0)}

@app.post("/api/info")
async def get_info(request: VideoInfoRequest):
    with yt_dlp.YoutubeDL(get_base_ydl_opts()) as ydl:
        try:
            info = ydl.extract_info(request.url.strip(), download=False)
            return {
                "title": info.get('title', 'Video'),
                "duration_seconds": int(info.get('duration') or 0),
                "thumbnail_url": info.get('thumbnail', ''),
                "uploader": info.get('uploader', 'Unknown')
            }
        except Exception as e:
            logger.error(f"Erro Info: {e}")
            raise HTTPException(status_code=400, detail="Erro ao acessar vídeo.")

@app.post("/api/download")
async def download(request: VideoDownloadRequest):
    unique_id = uuid.uuid4().hex[:8]
    progress_db[unique_id] = 0
    
    opts = get_base_ydl_opts()
    # Definimos um prefixo fixo no nome do arquivo para facilitar a busca
    base_name = f"vengine_{unique_id}"
    opts['outtmpl'] = str(TEMP_DIR / f"{base_name}_%(id)s.%(ext)s")

    if request.format_type == "mp3":
        opts.update({
            'format': 'bestaudio/best',
            'writethumbnail': True,
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                {'key': 'FFmpegEmbedThumbnail'},
                {'key': 'FFmpegMetadata'},
            ],
        })
    else:
        opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]

    def run_dl():
        with yt_dlp.YoutubeDL(opts) as ydl_instance:
            ydl_instance.params['v_engine_id'] = unique_id
            return ydl_instance.extract_info(request.url.strip(), download=True)

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, run_dl)
        
        # Aguarda um instante para o SO liberar o arquivo após o processamento do FFmpeg
        await asyncio.sleep(1.5)
        
        # Localiza o arquivo na pasta TEMP que comece com o nosso base_name
        files = list(TEMP_DIR.glob(f"{base_name}*"))
        
        # Filtra para pegar apenas arquivos de áudio/vídeo (ignora .jpg/.webp da thumb)
        media_exts = ['.mp3', '.mp4', '.m4v', '.webm', '.mkv']
        media_files = [f for f in files if f.suffix.lower() in media_exts]

        if not media_files:
            raise Exception("Arquivo final não localizado no servidor.")

        temp_file = media_files[0]
        final_ext = ".mp3" if request.format_type == "mp3" else ".mp4"
        final_name = f"final_{unique_id}{final_ext}"
        final_path = DOWNLOAD_DIR / final_name
        
        shutil.move(str(temp_file), str(final_path))
        
        # Limpeza rápida de resíduos de thumbnails
        for f in files:
            if f.exists():
                try: os.remove(f)
                except: pass

        return {
            "status": "success",
            "download_id": unique_id,
            "download_url": f"/api/file/{final_name}?title={sanitize_filename(info.get('title','video'))}"
        }
    except Exception as e:
        logger.error(f"Erro Download: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/file/{filename}")
async def get_file(filename: str, title: Optional[str] = "video"):
    p = DOWNLOAD_DIR / filename
    if not p.exists(): raise HTTPException(404, "Arquivo indisponível.")
    return FileResponse(p, media_type="application/octet-stream", filename=f"{title}{p.suffix}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
