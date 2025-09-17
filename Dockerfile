FROM python:3.11-slim

# Install system packages
RUN apt-get update && apt-get install -y ffmpeg

# Set workdir
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Start the bot
CMD ["python", "main.py"]
