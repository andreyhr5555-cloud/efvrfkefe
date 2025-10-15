FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt
# expose port for webhook / health
EXPOSE 8080
CMD ["python","bot.py"]
