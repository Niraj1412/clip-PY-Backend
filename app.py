from flask import Flask, request, jsonify
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig
import os
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv
from flask_cors import CORS
import requests
import json
import ffmpeg
import tempfile
from pathlib import Path
import time
import subprocess
import sys
import shutil
import boto3
from botocore.exceptions import NoCredentialsError
import uuid
import yt_dlp
import traceback

load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# AWS S3 Configuration
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_S3_BUCKET = os.getenv('AWS_S3_BUCKET', 'clipsmart')

# Initialize S3 client
s3_client = boto3.client(
    's3',
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

# Cookie configuration
BASE_DIR = '/app' if os.path.exists('/app') else os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(BASE_DIR, 'youtube_cookies.txt')
VALID_COOKIE_HEADERS = [
    '# HTTP Cookie File',
    '# Netscape HTTP Cookie File'
]

def validate_cookies_file(cookies_path):
    """Validate the cookies file format and size"""
    if not os.path.exists(cookies_path) or os.path.getsize(cookies_path) < 100:
        return False
    try:
        with open(cookies_path, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            return any(first_line.startswith(header) for header in VALID_COOKIE_HEADERS)
    except Exception:
        return False

# Check if ffmpeg is available
def check_ffmpeg_availability():
    try:
        # Check if ffmpeg is in PATH
        ffmpeg_path = shutil.which('ffmpeg')
        if ffmpeg_path:
            return True, ffmpeg_path
        
        # On Windows, try checking common installation locations
        if sys.platform == 'win32':
            common_paths = [
                str(Path(__file__).parent / "ffmpeg" / "bin" / "ffmpeg.exe"),
                str(Path(__file__).parent.parent / "ffmpeg" / "bin" / "ffmpeg.exe"),
                r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
                r".\ffmpeg\bin\ffmpeg.exe"
            ]
            for path in common_paths:
                if os.path.exists(path):
                    return True, path
                    
        # On Linux/EC2, check common locations
        if sys.platform == 'linux' or sys.platform == 'linux2':
            common_paths = [
                "/usr/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
                "/bin/ffmpeg",
                "./ffmpeg"
            ]
            for path in common_paths:
                if os.path.exists(path):
                    return True, path
        
        return False, None
    except Exception as e:
        print(f"Error checking ffmpeg: {str(e)}")
        return False, None

ffmpeg_available, ffmpeg_path = check_ffmpeg_availability()
if not ffmpeg_available:
    print("WARNING: ffmpeg executable not found. Video processing will not work.")
else:
    print(f"Found ffmpeg at: {ffmpeg_path}")

# Create necessary directories
if os.path.exists('/app'):
    BASE_DIR = '/app'
    print("Running in EC2/container environment with base directory: /app")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    print(f"Running in development environment with base directory: {BASE_DIR}")

DOWNLOAD_DIR = os.path.join(BASE_DIR, 'Download')
TMP_DIR = os.path.join(BASE_DIR, 'tmp')

# Ensure directories exist and have proper permissions
for directory in [DOWNLOAD_DIR, TMP_DIR]:
    try:
        os.makedirs(directory, exist_ok=True)
        if not os.access(directory, os.W_OK):
            try:
                os.chmod(directory, 0o755)
                print(f"Set permissions for {directory}")
            except Exception as e:
                print(f"WARNING: Cannot set permissions for {directory}: {str(e)}")
        print(f"Directory created and ready: {directory}")
    except Exception as e:
        print(f"ERROR: Failed to create or access directory {directory}: {str(e)}")

# Configure CORS
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS", "HEAD"],
        "allow_headers": ["Content-Type", "Authorization", "Access-Control-Allow-Origin",
                         "Access-Control-Allow-Headers", "Origin", "Accept", "X-Requested-With"],
        "expose_headers": ["Content-Type", "Authorization"],
        "supports_credentials": True,
        "max_age": 3600
    }
})

YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
WEBSHARE_USERNAME = os.getenv('WEBSHARE_USERNAME', 'otntczny')
WEBSHARE_PASSWORD = os.getenv('WEBSHARE_PASSWORD', '1w8maa9o5q5r')
PORT = int(os.getenv('PORT', 8000))

# Function to upload file to S3
def upload_to_s3(file_path, bucket, object_name=None):
    """Upload a file to an S3 bucket"""
    if object_name is None:
        object_name = os.path.basename(file_path)
    
    try:
        s3_client.upload_file(file_path, bucket, object_name)
        presigned_url = s3_client.generate_presigned_url('get_object',
                                                        Params={'Bucket': bucket,
                                                                'Key': object_name},
                                                        ExpiresIn=604800)
        return True, presigned_url
    except Exception as e:
        print(f"Error uploading to S3: {str(e)}")
        return False, None

@app.route('/')
def home():
    return jsonify({
        'message': 'ClipSmart API is running',
        'status': True
    })

@app.route('/transcript/<video_id>', methods=['GET', 'POST'])
def get_transcript(video_id):
    try:
        if not video_id:
            return jsonify({
                'message': "Video ID is required",
                'status': False
            }), 400

        # Fetch transcript
        transcript_list = None
        transcript_error = None

        try:
            ytt_api = YouTubeTranscriptApi(
                proxy_config=WebshareProxyConfig(
                    proxy_username=WEBSHARE_USERNAME,
                    proxy_password=WEBSHARE_PASSWORD,
                )
            )
            transcript_list = ytt_api.fetch(
                video_id,
                languages=['en']
            )
        except Exception as e:
            transcript_error = str(e)
            return jsonify({
                'message': "No transcript available for this video. The video might not have captions enabled.",
                'error': transcript_error,
                'status': False
            }), 404

        if not transcript_list:
            return jsonify({
                'message': "No transcript segments found for this video.",
                'status': False
            }), 404

        processed_transcript = []
        for index, item in enumerate(transcript_list):
            try:
                text = getattr(item, 'text', None)
                start = getattr(item, 'start', None)
                duration = getattr(item, 'duration', None)

                if text is not None and start is not None and duration is not None:
                    segment = {
                        'id': index + 1,
                        'text': text.strip(),
                        'startTime': float(start),
                        'endTime': float(start + duration),
                        'duration': float(duration)
                    }
                    if segment['text']:
                        processed_transcript.append(segment)
            except Exception:
                continue

        if not processed_transcript:
            return jsonify({
                'message': "Failed to process transcript segments.",
                'status': False
            }), 404

        return jsonify({
            'message': "Transcript fetched successfully",
            'data': processed_transcript,
            'status': True,
            'totalSegments': len(processed_transcript),
            'metadata': {
                'videoId': video_id,
                'language': 'en',
                'isAutoGenerated': True
            }
        }), 200

    except Exception as error:
        return jsonify({
            'message': "Failed to fetch transcript",
            'error': str(error),
            'status': False
        }), 500

def safe_ffmpeg_process(input_path, output_path, start_time, end_time):
    """Helper function to safely process video clips with ffmpeg"""
    try:
        cmd = [
            ffmpeg_path if ffmpeg_path else 'ffmpeg',
            '-i', input_path,
            '-ss', str(start_time),
            '-to', str(end_time),
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-y', output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        raise Exception(f"FFmpeg processing failed: {e.stderr.decode()}")
    except Exception as e:
        raise Exception(f"FFmpeg error: {str(e)}")

def download_via_ytdlp(video_id, input_path, use_cookies=True):
    """Download video using yt-dlp with improved error handling and retries"""
    ydl_opts = {
        'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/mp4/best[height<=720]',
        'outtmpl': input_path,
        'quiet': False,
        'no_warnings': False,
        'retries': 10,
        'fragment_retries': 10,
        'extractor_retries': 3,
        'ignoreerrors': False,
        'noprogress': True,
        'nooverwrites': False,
        'continuedl': False,
        'nopart': True,
        'windowsfilenames': sys.platform == 'win32',
        'paths': {
            'home': DOWNLOAD_DIR,
            'temp': TMP_DIR
        }
    }
    
    if use_cookies and validate_cookies_file(COOKIES_FILE):
        ydl_opts['cookiefile'] = COOKIES_FILE
    else:
        ydl_opts['http_headers'] = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.5'
        }
    
    try:
        os.makedirs(os.path.dirname(input_path), exist_ok=True)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
        
        if not os.path.exists(input_path):
            raise Exception("Downloaded file not found")
            
        if os.path.getsize(input_path) < 1024:
            raise Exception("Downloaded file is too small or empty")
            
        return True
    except Exception as e:
        print(f"yt-dlp download failed: {str(e)}")
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except:
                pass
        return False

def download_video(video_id, input_path):
    """Attempt to download video using multiple methods"""
    max_attempts = 3
    last_error = None
    
    for attempt in range(max_attempts):
        try:
            if download_via_ytdlp(video_id, input_path, use_cookies=True):
                return True
        except Exception as e:
            last_error = e
            time.sleep(2)  # Wait before retrying
            continue
    
    raise Exception(f"Failed to download video after {max_attempts} attempts: {str(last_error)}")

@app.route('/merge-clips', methods=['POST'])
def merge_clips_route():
    try:
        if not ffmpeg_available:
            return jsonify({
                'error': 'ffmpeg not available',
                'status': False
            }), 500
            
        data = request.get_json()
        if not data:
            return jsonify({
                'error': 'No data provided',
                'status': False
            }), 400
            
        clips = data.get('clips', [])
        if not clips:
            return jsonify({
                'error': 'No clips provided',
                'status': False
            }), 400

        # Validate each clip
        for clip in clips:
            if not isinstance(clip, dict):
                return jsonify({
                    'error': 'Invalid clip format - expected dictionary',
                    'status': False
                }), 400
                
            if not clip.get('videoId'):
                return jsonify({
                    'error': 'Missing videoId in clip',
                    'status': False
                }), 400
                
            try:
                start_time = float(clip.get('startTime', 0))
                end_time = float(clip.get('endTime', 0))
                if end_time <= start_time:
                    return jsonify({
                        'error': f'Invalid time range: start_time ({start_time}) must be less than end_time ({end_time})',
                        'status': False
                    }), 400
            except ValueError:
                return jsonify({
                    'error': 'Invalid startTime or endTime - must be numbers',
                    'status': False
                }), 400

        timestamp = int(time.time())
        file_list_path = os.path.join(TMP_DIR, f'filelist_{timestamp}.txt')
        output_path = os.path.join(TMP_DIR, f'merged_clips_{timestamp}.mp4')
        processed_clips = []
        
        try:
            # Process each clip
            for clip in clips:
                video_id = clip.get('videoId')
                start_time = float(clip.get('startTime', 0))
                end_time = float(clip.get('endTime', 0))
                
                input_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
                
                # Download video if needed
                if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
                    print(f"Downloading video {video_id}")
                    if not download_video(video_id, input_path):
                        raise Exception(f"Failed to download video {video_id}")
                
                # Create trimmed clip
                clip_output = os.path.join(TMP_DIR, f'clip_{video_id}_{int(start_time)}_{int(end_time)}.mp4')
                
                if not safe_ffmpeg_process(input_path, clip_output, start_time, end_time):
                    raise Exception(f"Failed to process clip {video_id}")
                
                processed_clips.append({
                    'path': clip_output,
                    'info': clip
                })

            if not processed_clips:
                raise ValueError("No clips were successfully processed")
                
            # Create file list for concatenation
            with open(file_list_path, 'w', encoding='utf-8') as f:
                for clip in processed_clips:
                    f.write(f"file '{clip['path']}'\n")

            time.sleep(1)  # Allow file handles to release

            # Merge clips
            cmd = [
                ffmpeg_path if ffmpeg_path else 'ffmpeg',
                '-f', 'concat',
                '-safe', '0',
                '-i', file_list_path,
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-y', output_path
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode != 0:
                raise Exception(result.stderr)
            
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise Exception("Merged file is empty or missing")
            
            # Upload to S3
            unique_filename = f"merged_{uuid.uuid4()}.mp4"
            s3_client.upload_file(
                output_path,
                AWS_S3_BUCKET,
                unique_filename,
                ExtraArgs={
                    'ContentType': 'video/mp4',
                    'ACL': 'public-read'
                }
            )
            
            # Generate presigned URL
            s3_url = s3_client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': AWS_S3_BUCKET,
                    'Key': unique_filename
                },
                ExpiresIn=604800  # 7 days expiration
            )

            return jsonify({
                'message': 'Clips merged successfully',
                's3Url': s3_url,
                'clipsInfo': [clip['info'] for clip in processed_clips],
                'success': True,
                'status': True,
                'fileNames3': unique_filename
            })

        except Exception as e:
            print(f"Error processing merge request: {str(e)}")
            traceback.print_exc()
            return jsonify({
                'error': str(e),
                'status': False,
                'type': 'processing_error'
            }), 500
            
        finally:
            # Cleanup
            def safe_remove(path):
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
            
            for clip in processed_clips:
                safe_remove(clip.get('path'))
            
            safe_remove(file_list_path)
            safe_remove(output_path)

    except Exception as e:
        print(f"Unhandled exception in /merge-clips route:")
        traceback.print_exc()
        return jsonify({
            'error': f"Internal server error: {str(e)}",
            'status': False,
            'type': 'unexpected_error'
        }), 500
        
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8000)))