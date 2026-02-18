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

# CONFIGURAÇÃO DE AMBIENTE
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path("downloads")
TEMP_DIR = Path("temp")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

def sanitize_filename(name: str) -> str:
    """Limpa o nome do arquivo para evitar erros no sistema."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name.replace(' ', '_').strip()[:100] or "video_download"

def get_base_ydl_opts() -> Dict[str, Any]:
    """Configurações base para burlar bloqueios em diversos sites."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        # User-agent moderno para parecer um navegador real
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        # Necessário para Instagram/TikTok e YouTube (via Secret File no Render)
        'cookiefile': 'cookies.txt' if os.path.exists("cookies.txt") else None,
        'concurrent_fragment_downloads': 5,
    }
    if opts['cookiefile']:
        logger.info("🍪 Cookies detectados e aplicados.")
    return opts

# MODELOS DE DADOS
class VideoInfoRequest(BaseModel):
    url: str

class VideoDownloadRequest(BaseModel):
    url: str
    format_type: str = "mp4" # mp4 ou mp3

app = FastAPI()

# Permite que o seu index.html fale com a API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    p = Path("index.html")
    return p.read_text(encoding="utf-8") if p.exists() else "API Online"

# 1. ROTA DE ANÁLISE (Suporta milhares de sites)
@app.post("/api/info")
async def get_info(request: VideoInfoRequest):
    opts = get_base_ydl_opts()
    # 'extract_flat' agiliza a análise e evita erros de formato inicial
    opts.update({'extract_flat': False, 'skip_download': True})
    
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            url = request.url.strip()
            info = ydl.extract_info(url, download=False)
            
            return {
                "title": info.get('title', 'Vídeo sem título'),
                "duration_seconds": int(info.get('duration') or 0),
                "thumbnail_url": info.get('thumbnail', ''),
                "uploader": info.get('uploader', info.get('webpage_url_domain', 'Desconhecido')),
                "formats": [] 
            }
        except Exception as e:
            logger.error(f"Erro na análise: {e}")
            raise HTTPException(status_code=400, detail="Não foi possível analisar este link. Verifique se o vídeo é público.")

# 2. ROTA DE DOWNLOAD (Conversão automática para MP4/MP3)
@app.post("/api/download")
async def download(request: VideoDownloadRequest, bg: BackgroundTasks):
    unique_id = uuid.uuid4().hex[:8]
    opts = get_base_ydl_opts()
    opts['outtmpl'] = str(TEMP_DIR / f"dl_{unique_id}_%(id)s.%(ext)s")
    
    # Configura o formato baseado na escolha do usuário
    if request.format_type == "mp3":
        opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        })
    else:
        # Tenta pegar o melhor MP4 ou converte se for outro formato (como MKV/WebM)
        opts.update({
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]
        })

    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(request.url.strip(), download=True)
            temp_file = Path(ydl.prepare_filename(info))
            
            # Ajusta extensão se o post-processor mudou (ex: de .webm para .mp4 ou .mp3)
            if request.format_type == "mp3":
                temp_file = temp_file.with_suffix(".mp3")
            elif not temp_file.exists():
                temp_file = temp_file.with_suffix(".mp4")
            
            final_name = f"vengine_{uuid.uuid4().hex[:6]}{temp_file.suffix}"
            final_path = DOWNLOAD_DIR / final_name
            
            # Move para a pasta de downloads final
            if temp_file.exists():
                shutil.move(str(temp_file), str(final_path))
            else:
                # Busca fallback caso o nome tenha mudado drasticamente
                search = list(TEMP_DIR.glob(f"dl_{unique_id}*"))
                if search: shutil.move(str(search[0]), str(final_path))
                else: raise Exception("Arquivo não encontrado após processamento.")

            return {
                "status": "success", 
                "download_url": f"/api/file/{final_name}?title={sanitize_filename(info.get('title','video'))}"
            }
        except Exception as e:
            logger.error(f"Erro no download: {e}")
            raise HTTPException(status_code=500, detail="Erro ao processar vídeo. Tente uma qualidade menor.")

# 3. ROTA DE ENTREGA DO ARQUIVO
@app.get("/api/file/{filename}")
async def get_file(filename: str, title: Optional[str] = "video"):
    p = DOWNLOAD_DIR / filename
    if not p.exists():
        raise HTTPException(404, "O link de download expirou.")
    return FileResponse(p, media_type="application/octet-stream", filename=f"{title}{p.suffix}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
