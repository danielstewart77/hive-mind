#!/bin/bash

# Pull the latest open-webui image
sudo docker compose pull open-webui

# Start the container in detached mode and build if needed
sudo docker compose up -d --build open-webui

#clean up unused images
sudo docker image prune -a
