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
            # Extrai a porcentagem numérica
            p = d.get('_percent_str', '0%').replace('%', '').strip()
            # O yt-dlp às vezes manda cores ANSI na string, limpamos se necessário
            p = re.sub(r'\x1b\[[0-9;]*m', '', p) 
            
            # O download_id é passado via params pelo yt-dlp
            download_id = d.get('info_dict', {}).get('v_engine_id')
            if download_id:
                progress_db[download_id] = float(p)
        except Exception:
            pass
    elif d['status'] == 'finished':
        download_id = d.get('info_dict', {}).get('v_engine_id')
        if download_id:
            progress_db[download_id] = 100.0

# 🔐 CONFIGURAÇÃO COM COOKIES
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
        logger.info("🍪 SUCESSO: Arquivo cookies.txt carregado!")
    else:
        logger.warning("⚠️ AVISO: cookies.txt não encontrado.")
        
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

# ROTA PARA O FRONT-END CONSULTAR O PROGRESSO
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
            logger.error(f"Erro YT-DLP: {e}")
            raise HTTPException(status_code=400, detail="Erro ao acessar vídeo.")

@app.post("/api/download")
async def download(request: VideoDownloadRequest):
    unique_id = uuid.uuid4().hex[:8]
    progress_db[unique_id] = 0
    
    opts = get_base_ydl_opts()
    opts['outtmpl'] = str(TEMP_DIR / f"dl_{unique_id}_%(id)s.%(ext)s")
    
    # Passamos o ID para dentro do info_dict para o hook reconhecer
    # yt-dlp permite metadados customizados
    opts['params'] = {'v_engine_id': unique_id}

    if request.format_type == "mp3":
        opts.update({
            'format': 'bestaudio/best',
            'writethumbnail': True, # Baixa a thumb para embutir
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                {'key': 'FFmpegEmbedThumbnail'}, # EMBUTE A CAPA
                {'key': 'FFmpegMetadata'},       # ADICIONA TAGS
            ],
        })
    else:
        opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]

    def run_dl():
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Injetamos o ID manualmente no dicionário de params para o hook
            ydl.params['v_engine_id'] = unique_id
            return ydl.extract_info(request.url.strip(), download=True)

    try:
        # Executa o download em uma thread separada para não bloquear o loop do FastAPI
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, run_dl)
        
        temp_file = Path(ydl.prepare_filename(info))
        if request.format_type == "mp3":
            temp_file = temp_file.with_suffix(".mp3")

        final_name = f"final_{unique_id}{temp_file.suffix}"
        final_path = DOWNLOAD_DIR / final_name
        
        if temp_file.exists():
            shutil.move(str(temp_file), str(final_path))
        else:
            # Fallback para encontrar o arquivo
            files = list(TEMP_DIR.glob(f"dl_{unique_id}*"))
            if files:
                # Filtrar arquivos de imagem que sobram do EmbedThumbnail
                media_files = [f for f in files if f.suffix in ['.mp3', '.mp4', '.m4a', '.webm']]
                shutil.move(str(media_files[0]), str(final_path))
        
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
