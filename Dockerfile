FROM python:3.12-slim

WORKDIR /app

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the rest of the app
COPY . .

# Run the Flask app
CMD ["python3", "app.py"]