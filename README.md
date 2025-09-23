# API for Screen Monitoring IM Project

A FastAPI-based REST API for managing screen monitoring data, user accounts, and activity logs.

## Features

- User management with secure password hashing (Argon2)
- Screenshot storage and retrieval via Supabase
- Activity logging and categorization
- User presence tracking
- Activity thresholds and alerts
- ZIP download support for screenshot collections

## API Endpoints

### Users
- `GET /users` - List all users
- `GET /users/{user_id}` - Get user details
- `POST /users` - Create new user
- `POST /auth/login` - User authentication
- `GET /users/{user_id}/presence` - Get user's online status
- `GET /users/{user_id}/thresholds` - Get user's activity thresholds

### Screenshots
- `GET /users/{user_id}/screenshots` - List user's screenshots
- `GET /screenshots/{screenshot_id}` - Get screenshot metadata
- `GET /screenshots/{screenshot_id}/download` - Download screenshot
- `GET /users/{user_id}/screenshots.zip` - Download multiple screenshots as ZIP

### Logs
- `GET /users/{user_id}/logs` - List activity logs
- `GET /users/{user_id}/logs/within/summary` - Get activity summary
- `GET /users/{user_id}/summary/{day}` - Get daily activity summary

## Setup
Currently running on: `http://sidekick1.lu.im.ntu.edu.tw:8000/`

## API Documentation

Once running, view the interactive API docs at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Categories

The system tracks activities in these categories:
- Gaming
- Videos
- Articles
- Messaging
- Social Media
- Text Editing
- Others

## Database Schema

The API uses PostgreSQL with these main tables:
- `users` - User accounts and settings
- `screenshots` - Screenshot metadata and storage info
- `logs` - Activity logs and categorization