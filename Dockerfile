# Use full Ubuntu image for maximum package compatibility
FROM ubuntu:latest

# Set the working directory
WORKDIR /usr/src/app

# Install Python and system dependencies
RUN apt update && apt install -y \
    python3 python3-pip python3-venv python3-dev gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Create and activate a virtual environment
RUN python3 -m venv /usr/src/app/venv
ENV PATH="/usr/src/app/venv/bin:$PATH"

# Upgrade pip to the latest version
RUN pip install --upgrade pip

# Manually install agent_tooling before other dependencies
RUN pip install --no-cache-dir --force-reinstall agent_tooling

# Copy requirements.txt and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --force-reinstall -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose the application port
EXPOSE 7977

# Run the application
CMD ["python", "main.py"]