# Usamos uma versão estável do Python
FROM python:3.11-slim

# Instala o FFmpeg e ferramentas de sistema necessárias
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Define a pasta de trabalho
WORKDIR /app

# Copia os requisitos e instala as dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante dos arquivos (app.py, index.html, etc)
COPY . .

# Cria a pasta de downloads com permissões certas
RUN mkdir -p downloads && chmod 777 downloads

# Comando para rodar a API (usando a porta padrão da Koyeb)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
