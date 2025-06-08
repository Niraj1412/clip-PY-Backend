from flask import Flask, request, jsonify
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig
import os
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv
from flask_cors import CORS
import requests
import os
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
import logging
from logging.handlers import RotatingFileHandler
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import shutil

load_dotenv()

app = Flask(__name__)
CORS(app)

def setup_logging():
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    logger = logging.getLogger('clip_merger')
    logger.setLevel(logging.INFO)
    
    # File handler with rotation
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, 'clip_merger.log'),
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    ))
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# Initialize logger
logger = setup_logging()


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        RotatingFileHandler('app.log', maxBytes=10*1024*1024, backupCount=5),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize rate limiter
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["5 per minute"]
)

# Initialize Flask app


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

def backup_cookies():
    """Create a backup of the cookies file"""
    try:
        if os.path.exists(COOKIES_FILE):
            shutil.copy2(COOKIES_FILE, COOKIES_BACKUP_FILE)
            return True
        return False
    except Exception as e:
        print(f"Error backing up cookies: {str(e)}")
        return False

def generate_youtube_cookies():
    """Generate YouTube cookies from browser with improved error handling"""
    try:
        # Try different browsers with proper paths
        browsers = [
            ('chrome', None),
            ('firefox', None),
            ('edge', None),
            ('brave', None),
            # Add Flatpak paths for Linux
            ('chrome', '~/.var/app/com.google.Chrome/config/google-chrome'),
            ('firefox', '~/.var/app/org.mozilla.firefox/.mozilla/firefox'),
            ('brave', '~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser')
        ]
        
        for browser, custom_path in browsers:
            try:
                cmd = [
                    sys.executable, "-m", "yt_dlp",
                    "--cookies-from-browser", 
                    f"{browser}" + (f":{custom_path}" if custom_path else ""),
                    "--cookies", COOKIES_FILE,
                    "--skip-download",
                    "--no-check-certificate",
                    "https://www.youtube.com"
                ]
                
                # Set timeout based on platform
                timeout = 120 if sys.platform == 'linux' else 60
                
                result = subprocess.run(
                    cmd, 
                    check=True, 
                    timeout=timeout,
                    capture_output=True,
                    text=True
                )
                
                # Validate the generated cookies file
                if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 100:
                    print(f"Successfully generated cookies from {browser}")
                    return True
                else:
                    print(f"Generated cookies file is invalid from {browser}")
                    print("Command output:", result.stdout)
                    print("Command error:", result.stderr)
                    
            except subprocess.TimeoutExpired:
                print(f"Timeout generating cookies from {browser}")
                continue
            except Exception as e:
                print(f"Error generating cookies from {browser}: {str(e)}")
                continue
                
        print("All cookie generation attempts failed")
        return False
        
    except Exception as e:
        print(f"Error in generate_youtube_cookies: {str(e)}")
        return False
    
def restore_cookies_backup():
    """Restore cookies from backup"""
    try:
        if os.path.exists(COOKIES_BACKUP_FILE):
            shutil.copy2(COOKIES_BACKUP_FILE, COOKIES_FILE)
            return True
        return False
    except Exception as e:
        print(f"Error restoring cookies backup: {str(e)}")
        return False

def validate_cookies_file(cookies_path):
    """Validate the cookies file format and size with more robust checks"""
    if not os.path.exists(cookies_path):
        print(f"Cookies file not found at {cookies_path}")
        return False
    
    file_size = os.path.getsize(cookies_path)
    if file_size < 100:
        print(f"Cookies file is too small ({file_size} bytes)")
        return False
    
    try:
        with open(cookies_path, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            
            # Check for Netscape format header
            if not any(first_line.startswith(valid) for valid in VALID_COOKIE_HEADERS):
                print(f"Invalid cookies file header: {first_line}")
                return False
            
            # Check for at least one valid cookie line
            valid_cookies = 0
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        valid_cookies += 1
            
            if valid_cookies == 0:
                print("No valid cookie entries found in file")
                return False
                
            return True
    except Exception as e:
        print(f"Error reading cookies file: {str(e)}")
        return False


def test_s3_connection():
    """Test if we can connect to S3"""
    try:
        s3_client.head_bucket(Bucket=AWS_S3_BUCKET)
        return True
    except Exception as e:
        logger.error(f"S3 connection test failed: {str(e)}")
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
                r"C:\Users\14nir\Downloads\ffmpeg-2025-04-23-git-25b0a8e295-full_build\ffmpeg-2025-04-23-git-25b0a8e295-full_build\bin\ffmpeg.exe",
                r"C:\Users\14nir\Downloads\ffmpeg-2025-04-23-git-25b0a8e295-full_build\bin\ffmpeg.exe",
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
    print("Please install ffmpeg and ensure it's in your system PATH.")
    print("On Windows, you can download ffmpeg from https://ffmpeg.org/download.html")
    print("On Linux, run 'apt-get install ffmpeg' or equivalent for your distribution")
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
    """Upload a file to an S3 bucket
    
    :param file_path: File to upload
    :param bucket: Bucket to upload to
    :param object_name: S3 object name. If not specified then file_name is used
    :return: True if file was uploaded, else False
    """
    # If S3 object_name was not specified, use file_name
    if object_name is None:
        object_name = os.path.basename(file_path)
    
    try:
        s3_client.upload_file(file_path, bucket, object_name)
        # Generate a presigned URL for the uploaded file
        presigned_url = s3_client.generate_presigned_url('get_object',
                                                        Params={'Bucket': bucket,
                                                                'Key': object_name},
                                                        ExpiresIn=604800)  # URL expires in 7 days
        return True, presigned_url
    except FileNotFoundError:
        print(f"The file {file_path} was not found")
        return False, None
    except NoCredentialsError:
        print("Credentials not available")
        return False, None
    except Exception as e:
        print(f"Error uploading to S3: {str(e)}")
        return False, None

@app.route('/')
def home():
    return jsonify({
        'message': 'ClipSmart API is running',
        'status': True
    })


@app.route('/getData/<video_id>', methods=['GET'])
def get_data(video_id):
    try:
        if not video_id:
            return jsonify({"error": "No videoID provided"}), 400

        api_url = f"https://ytstream-download-youtube-videos.p.rapidapi.com/dl?id={video_id}"
        headers = {
            'x-rapidapi-key': 'c58bbfe08bmsh4487c5af7cf106fp1fa8d9jsn95391a6d8a1f',
            'x-rapidapi-host': 'ytstream-download-youtube-videos.p.rapidapi.com'
        }

        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        result = response.json()

        adaptive_formats = result.get('adaptiveFormats', [])
        if not adaptive_formats or not isinstance(adaptive_formats, list) or not adaptive_formats[0].get('url'):
            return jsonify({"error": "Invalid or missing adaptiveFormats data"}), 400


        download_link = f"wget '{adaptive_formats[0]['url']}' -O './Download/{video_id}.mp4'"

        response = requests.get(adaptive_formats[0]['url'], stream=True)

        # Create Download directory if it doesn't exist
        os.makedirs("./Download", exist_ok=True)
        
        with open(f"./Download/{video_id}.mp4", "wb") as f:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)


        print("Video downloaded successfully!")

        return jsonify({
            "downloadURL" : download_link,
            "normalURL" : adaptive_formats[0]['url']
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
            except Exception as fallback_err:
                return jsonify({
                    'message': "No transcript available for this video. The video might not have captions enabled.",
                    'originalError': transcript_error,
                    'fallbackError': str(fallback_err),
                    'status': False
                }), 404

        if not transcript_list:
            return jsonify({
                'message': "No transcript segments found for this video. The video might not have captions.",
                'status': False
            }), 404

        processed_transcript = []
        for index, item in enumerate(transcript_list):
            try:
                # Access attributes directly from the FetchedTranscriptSnippet object
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
                'message': "Failed to process transcript segments. The transcript may be malformed.",
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

@app.route('/upload-cookies', methods=['POST'])
def upload_cookies():
    """
    Upload a cookies file to use with yt-dlp.
    The file must be in Mozilla/Netscape format and the first line must be 
    either '# HTTP Cookie File' or '# Netscape HTTP Cookie File'.
    """
    try:
        # Check if the POST request has the file part
        if 'cookiesFile' not in request.files:
            return jsonify({
                'message': "No cookies file provided",
                'status': False
            }), 400
            
        file = request.files['cookiesFile']
        
        # If the user does not select a file, the browser submits an
        # empty file without a filename
        if file.filename == '':
            return jsonify({
                'message': "No cookies file selected",
                'status': False
            }), 400
            
        # Save the file
        cookies_file = os.path.join(BASE_DIR, 'youtube_cookies.txt')
        file.save(cookies_file)
        
        # Validate the cookie file
        try:
            with open(cookies_file, 'r', encoding='utf-8') as f:
                first_line = f.readline().strip()
                if not (first_line.startswith('# HTTP Cookie File') or first_line.startswith('# Netscape HTTP Cookie File')):
                    os.remove(cookies_file)
                    return jsonify({
                        'message': "Invalid cookies file format. File must be in Mozilla/Netscape format.",
                        'status': False
                    }), 400
                    
            # If file is too small, it's probably invalid
            if os.path.getsize(cookies_file) < 100:
                os.remove(cookies_file)
                return jsonify({
                    'message': "Cookies file is too small to be valid",
                    'status': False
                }), 400
                
        except Exception as e:
            if os.path.exists(cookies_file):
                os.remove(cookies_file)
            return jsonify({
                'message': f"Error validating cookies file: {str(e)}",
                'status': False
            }), 500
            
        return jsonify({
            'message': "Cookies file uploaded successfully",
            'status': True
        }), 200
        
    except Exception as e:
        return jsonify({
            'message': f"Error uploading cookies file: {str(e)}",
            'status': False
        }), 500

@app.route('/generate-cookies', methods=['GET'])
def generate_cookies():
    """Generate a cookies file from the user's browser"""
    try:
        browser = request.args.get('browser', 'chrome')
        custom_path = request.args.get('custom_path', None)
        
        cookies_file = os.path.join(BASE_DIR, 'youtube_cookies.txt')
        
        # Remove existing cookies file if it exists
        if os.path.exists(cookies_file):
            os.remove(cookies_file)
        
        # Construct the command with proper Netscape format
        extract_cmd = [
            sys.executable, "-m", "yt_dlp", 
            "--cookies-from-browser", f"{browser}" + (f":{custom_path}" if custom_path else ""),
            "--cookies", cookies_file,
            "--skip-download",
            "--no-check-certificate",
            "--print", "requested_downloads",
            "https://www.youtube.com"
        ]
        
        print(f"Extracting cookies with command: {' '.join(extract_cmd)}")
        
        try:
            process = subprocess.run(
                extract_cmd, 
                capture_output=True, 
                text=True, 
                timeout=60
            )
            
            if process.returncode != 0:
                print(f"Cookie extraction failed with return code {process.returncode}")
                print("Stdout:", process.stdout)
                print("Stderr:", process.stderr)
                return jsonify({
                    'message': f"Failed to extract cookies from {browser}",
                    'status': False,
                    'stdout': process.stdout,
                    'stderr': process.stderr
                }), 400
            
            if not validate_cookies_file(cookies_file):
                return jsonify({
                    'message': "Generated cookies file is invalid",
                    'status': False,
                    'stdout': process.stdout,
                    'stderr': process.stderr
                }), 400
                
            return jsonify({
                'message': f"Successfully generated cookies file from {browser}",
                'status': True,
                'file_size': os.path.getsize(cookies_file),
                'platform': sys.platform
            }), 200
            
        except subprocess.TimeoutExpired:
            return jsonify({
                'message': "Extraction timed out. The browser profile might be locked or invalid.",
                'status': False
            }), 400
            
    except Exception as e:
        return jsonify({
            'message': f"Error generating cookies file: {str(e)}",
            'status': False,
            'traceback': traceback.format_exc()
        }), 500

@app.route('/check-cookies', methods=['GET'])
def check_cookies():
    """Check if a valid cookies file exists on the server and test it against YouTube."""
    try:
        cookies_file = os.path.join(BASE_DIR, 'youtube_cookies.txt')
        
        # Check if file exists and is not empty
        if not os.path.exists(cookies_file) or os.path.getsize(cookies_file) < 100:
            return jsonify({
                'message': "No valid cookies file found",
                'status': False,
                'has_cookies': False
            }), 200
            
        # Validate the cookie file format
        try:
            with open(cookies_file, 'r', encoding='utf-8') as f:
                first_line = f.readline().strip()
                if not (first_line.startswith('# HTTP Cookie File') or first_line.startswith('# Netscape HTTP Cookie File')):
                    return jsonify({
                        'message': "Cookies file exists but has invalid format",
                        'status': True,
                        'has_cookies': True,
                        'valid_format': False
                    }), 200
        except Exception as e:
            return jsonify({
                'message': f"Error reading cookies file: {str(e)}",
                'status': False,
                'has_cookies': True,
                'valid_format': False
            }), 200
            
        # Test the cookies with a quick yt-dlp request (just getting info, not downloading)
        try:
            test_cmd = [
                sys.executable, "-m", "yt_dlp",
                "--cookies", cookies_file,
                "--skip-download",
                "--print", "title",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # Use a popular video to test
            ]
            
            process = subprocess.run(test_cmd, capture_output=True, text=True, timeout=15)
            
            if process.returncode != 0 or "Sign in to confirm you're not a bot" in process.stderr:
                return jsonify({
                    'message': "Cookies exist but failed authentication test with YouTube",
                    'status': True,
                    'has_cookies': True,
                    'valid_format': True,
                    'works_with_youtube': False,
                    'error': process.stderr
                }), 200
                
            return jsonify({
                'message': "Valid cookies file found and working with YouTube",
                'status': True,
                'has_cookies': True,
                'valid_format': True,
                'works_with_youtube': True,
                'file_size': os.path.getsize(cookies_file),
                'last_modified': time.ctime(os.path.getmtime(cookies_file))
            }), 200
            
        except Exception as test_error:
            return jsonify({
                'message': f"Error testing cookies with YouTube: {str(test_error)}",
                'status': True,
                'has_cookies': True,
                'valid_format': True,
                'works_with_youtube': None,
                'error': str(test_error)
            }), 200
        
    except Exception as e:
        return jsonify({
            'message': f"Error checking cookies: {str(e)}",
            'status': False
        }), 500

@app.route('/set-browser-path', methods=['POST'])
def set_browser_path():
    """
    Set a custom browser path for cookie extraction.
    This is useful for environments where browsers are installed in non-standard locations.
    
    Expected JSON body:
    {
        "browser": "chrome|firefox|edge|brave|safari",
        "path": "/path/to/browser/profile"
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({
                'message': "No data provided",
                'status': False
            }), 400
            
        browser = data.get('browser')
        path = data.get('path')
        
        if not browser or not path:
            return jsonify({
                'message': "Browser name and path are required",
                'status': False
            }), 400
            
        # Normalize browser name
        browser = browser.lower()
        
        # Create a JSON file to store custom browser paths
        browser_config_file = os.path.join(BASE_DIR, 'browser_paths.json')
        
        # Load existing config or create new
        browser_paths = {}
        if os.path.exists(browser_config_file):
            try:
                with open(browser_config_file, 'r') as f:
                    browser_paths = json.load(f)
            except Exception as e:
                print(f"Error loading browser paths: {str(e)}")
                # Continue with empty config
        
        # Check if path exists
        if not os.path.exists(path):
            return jsonify({
                'message': f"Path does not exist: {path}",
                'status': False,
                'exists': False
            }), 400
        
        # Update config
        browser_paths[browser] = path
        
        # Save config
        try:
            with open(browser_config_file, 'w') as f:
                json.dump(browser_paths, f, indent=2)
        except Exception as e:
            return jsonify({
                'message': f"Error saving browser paths: {str(e)}",
                'status': False
            }), 500
        
        # Test if we can extract cookies using this path
        cookies_file = os.path.join(BASE_DIR, f'test_cookies_{browser}.txt')
        
        extract_cmd = [
            sys.executable, "-m", "yt_dlp", 
            "--cookies-from-browser", f"{browser}:{path}",
            "--cookies", cookies_file,
            "--skip-download",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ]
        
        try:
            print(f"Testing cookie extraction from {browser} at {path}")
            process = subprocess.run(extract_cmd, capture_output=True, text=True, timeout=30)
            
            # Check if test was successful
            extraction_success = False
            if os.path.exists(cookies_file) and os.path.getsize(cookies_file) > 100:
                extraction_success = True
                print(f"Successfully extracted test cookies from {browser}")
                # Clean up test file
                try:
                    os.remove(cookies_file)
                except:
                    pass
            else:
                print(f"Failed to extract test cookies from {browser}: {process.stderr}")
        except Exception as test_error:
            extraction_success = False
            print(f"Error testing cookie extraction: {str(test_error)}")
        
        return jsonify({
            'message': f"Browser path set successfully for {browser}",
            'status': True,
            'browser': browser,
            'path': path,
            'extraction_test': extraction_success
        }), 200
        
    except Exception as e:
        return jsonify({
            'message': f"Error setting browser path: {str(e)}",
            'status': False,
            'traceback': traceback.format_exc(),
            'platform': sys.platform
        }), 500

@app.route('/cleanup-downloads', methods=['POST'])
def cleanup_downloads():
    """
    Clean up the Download folder to free up disk space.
    
    POST parameters:
    - mode: (string) The cleanup mode: 'all' (remove all files), 'mp4only' (remove only MP4 files)
    - dryRun: (boolean) If true, only show what would be deleted without actually deleting
    
    Returns the count and details of files that were or would be removed.
    """
    try:
        data = request.get_json() or {}
        mode = data.get('mode', 'mp4only')  # Default to removing only MP4 files
        dry_run = data.get('dryRun', False) # Default to actually deleting files
        
        # Format size for human readability
        def format_size(size_bytes):
            if size_bytes < 1024:
                return f"{size_bytes} bytes"
            elif size_bytes < 1024 * 1024:
                return f"{size_bytes / 1024:.2f} KB"
            elif size_bytes < 1024 * 1024 * 1024:
                return f"{size_bytes / (1024 * 1024):.2f} MB"
            else:
                return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
        
        # Validate mode
        if mode not in ['all', 'mp4only']:
            return jsonify({
                'message': "Invalid mode. Must be 'all' or 'mp4only'",
                'status': False
            }), 400
            
        # Get list of files in the Download directory
        if not os.path.exists(DOWNLOAD_DIR):
            return jsonify({
                'message': "Download directory does not exist",
                'status': False
            }), 404
            
        files = os.listdir(DOWNLOAD_DIR)
        to_delete = []
        skipped = []
        
        # Filter files based on mode
        for filename in files:
            file_path = os.path.join(DOWNLOAD_DIR, filename)
            if not os.path.isfile(file_path):
                # Skip directories
                continue
                
            if mode == 'all' or (mode == 'mp4only' and filename.endswith('.mp4')):
                file_info = {
                    'name': filename,
                    'path': file_path,
                    'size': os.path.getsize(file_path),
                    'modified': time.ctime(os.path.getmtime(file_path))
                }
                to_delete.append(file_info)
            else:
                skipped.append(filename)
        
        # Calculate total size to be freed
        total_size = sum(file['size'] for file in to_delete)
        
        # Perform deletion if not a dry run
        deleted = []
        errors = []
        
        if not dry_run:
            for file_info in to_delete:
                try:
                    os.remove(file_info['path'])
                    deleted.append(file_info['name'])
                except Exception as e:
                    errors.append({
                        'file': file_info['name'],
                        'error': str(e)
                    })
        
        return jsonify({
            'message': "Cleanup completed successfully" if not dry_run else "Dry run completed successfully",
            'status': True,
            'mode': mode,
            'dryRun': dry_run,
            'totalFiles': len(to_delete),
            'totalSize': total_size,
            'totalSizeFormatted': format_size(total_size),
            'deleted': deleted if not dry_run else [],
            'toDelete': [f['name'] for f in to_delete] if dry_run else [],
            'skipped': skipped,
            'errors': errors
        }), 200
        
    except Exception as e:
        return jsonify({
            'message': f"Error during cleanup: {str(e)}",
            'status': False,
            'traceback': traceback.format_exc()
        }), 500

@app.route('/download-folder-status', methods=['GET'])
def download_folder_status():
    """
    Get the current status of the Download folder, including file list and disk usage.
    
    Optional query parameters:
    - includeDetails: (boolean) If true, include detailed info about each file
    - filter: (string) File extension filter (e.g., 'mp4' to show only MP4 files)
    """
    try:
        include_details = request.args.get('includeDetails', 'false').lower() == 'true'
        file_filter = request.args.get('filter', '').lower()
        
        # Format size for human readability
        def format_size(size_bytes):
            if size_bytes < 1024:
                return f"{size_bytes} bytes"
            elif size_bytes < 1024 * 1024:
                return f"{size_bytes / 1024:.2f} KB"
            elif size_bytes < 1024 * 1024 * 1024:
                return f"{size_bytes / (1024 * 1024):.2f} MB"
            else:
                return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
        
        if not os.path.exists(DOWNLOAD_DIR):
            return jsonify({
                'message': "Download directory does not exist",
                'status': False
            }), 404
            
        # Get list of files
        files_info = []
        total_size = 0
        file_counts = {
            'mp4': 0,
            'part': 0,
            'other': 0
        }
        
        for filename in os.listdir(DOWNLOAD_DIR):
            file_path = os.path.join(DOWNLOAD_DIR, filename)
            
            # Skip directories
            if not os.path.isfile(file_path):
                continue
                
            # Apply filter if specified
            if file_filter and not filename.lower().endswith(f'.{file_filter}'):
                continue
                
            # Get file size
            file_size = os.path.getsize(file_path)
            total_size += file_size
            
            # Count file by type
            if filename.lower().endswith('.mp4'):
                file_counts['mp4'] += 1
            elif filename.lower().endswith('.part'):
                file_counts['part'] += 1
            else:
                file_counts['other'] += 1
            
            # Add detailed info if requested
            if include_details:
                file_info = {
                    'name': filename,
                    'size': file_size,
                    'sizeFormatted': format_size(file_size),
                    'modified': time.ctime(os.path.getmtime(file_path)),
                    'modifiedTimestamp': os.path.getmtime(file_path)
                }
                files_info.append(file_info)
        
        # Get disk usage for the partition
        try:
            if sys.platform == 'win32':
                # On Windows
                drive = os.path.splitdrive(DOWNLOAD_DIR)[0]
                if not drive:
                    drive = os.path.splitdrive(os.getcwd())[0]
                
                import ctypes
                free_bytes = ctypes.c_ulonglong(0)
                total_bytes = ctypes.c_ulonglong(0)
                ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                    ctypes.c_wchar_p(drive), None, ctypes.pointer(total_bytes), ctypes.pointer(free_bytes)
                )
                disk_info = {
                    'totalSpace': total_bytes.value,
                    'freeSpace': free_bytes.value,
                    'usedSpace': total_bytes.value - free_bytes.value,
                    'totalSpaceFormatted': format_size(total_bytes.value),
                    'freeSpaceFormatted': format_size(free_bytes.value),
                    'usedSpaceFormatted': format_size(total_bytes.value - free_bytes.value)
                }
            else:
                # On Unix/Linux
                import shutil
                usage = shutil.disk_usage(DOWNLOAD_DIR)
                disk_info = {
                    'totalSpace': usage.total,
                    'freeSpace': usage.free,
                    'usedSpace': usage.used,
                    'totalSpaceFormatted': format_size(usage.total),
                    'freeSpaceFormatted': format_size(usage.free),
                    'usedSpaceFormatted': format_size(usage.used)
                }
        except Exception as disk_error:
            disk_info = {
                'error': str(disk_error)
            }
        
        # Return the folder information
        result = {
            'status': True,
            'path': DOWNLOAD_DIR,
            'totalFiles': file_counts['mp4'] + file_counts['part'] + file_counts['other'],
            'mp4Files': file_counts['mp4'],
            'partFiles': file_counts['part'],
            'otherFiles': file_counts['other'],
            'totalSize': total_size,
            'totalSizeFormatted': format_size(total_size),
            'diskInfo': disk_info
        }
        
        # Add file details if requested
        if include_details:
            # Sort files by size (largest first)
            files_info.sort(key=lambda x: x['size'], reverse=True)
            result['files'] = files_info
        
        return jsonify(result), 200
        
    except Exception as e:
        return jsonify({
            'message': f"Error getting Download folder status: {str(e)}",
            'status': False,
            'traceback': traceback.format_exc()
        }), 500

def safe_ffmpeg_process(input_path, output_path, start_time, end_time, max_attempts=3):
    """Robust FFmpeg processing with multiple fallback methods and thorough validation
    
    Args:
        input_path: Path to input video file
        output_path: Path for processed output file
        start_time: Start time in seconds
        end_time: End time in seconds
        max_attempts: Number of retry attempts per method
        
    Returns:
        bool: True if processing succeeded, False otherwise
        
    Raises:
        Exception: If all processing methods fail after retries
    """
    # Enhanced input validation
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")
    
    min_file_size = 1024  # 1KB minimum file size
    if os.path.getsize(input_path) < min_file_size:
        raise ValueError(f"Input file too small (size: {os.path.getsize(input_path)} bytes)")
    
    # Validate time parameters
    if end_time <= start_time:
        raise ValueError(f"Invalid time range (start: {start_time}, end: {end_time})")
    
    # Processing methods with different approaches
    methods = [
        {  # Method 1: Stream copy (fastest)
            'name': 'stream_copy',
            'cmd': [
                ffmpeg_path if ffmpeg_path else 'ffmpeg',
                '-i', input_path,
                '-ss', str(start_time),
                '-to', str(end_time),
                '-c', 'copy',  # Stream copy
                '-movflags', 'faststart',
                '-y', output_path
            ],
            'timeout': 60  # 1 minute timeout
        },
        {  # Method 2: Re-encode with h264
            'name': 'reencode_h264',
            'cmd': [
                ffmpeg_path if ffmpeg_path else 'ffmpeg',
                '-i', input_path,
                '-ss', str(start_time),
                '-to', str(end_time),
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-movflags', 'faststart',
                '-y', output_path
            ],
            'timeout': 300  # 5 minute timeout
        },
        {  # Method 3: Error-resilient re-encode
            'name': 'error_resilient',
            'cmd': [
                ffmpeg_path if ffmpeg_path else 'ffmpeg',
                '-err_detect', 'ignore_err',
                '-i', input_path,
                '-ss', str(start_time),
                '-to', str(end_time),
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-movflags', 'faststart',
                '-fflags', '+genpts',  # Generate missing PTS
                '-y', output_path
            ],
            'timeout': 600  # 10 minute timeout
        },
        {  # Method 4: Two-pass encoding (highest quality fallback)
            'name': 'two_pass',
            'cmd': [
                ffmpeg_path if ffmpeg_path else 'ffmpeg',
                '-i', input_path,
                '-ss', str(start_time),
                '-to', str(end_time),
                '-c:v', 'libx264',
                '-preset', 'medium',
                '-crf', '18',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-movflags', 'faststart',
                '-y', output_path
            ],
            'timeout': 900  # 15 minute timeout
        }
    ]
    
    # Try each method with retries
    for method in methods:
        for attempt in range(max_attempts):
            try:
                # Clean up any previous failed output
                if os.path.exists(output_path):
                    os.remove(output_path)
                
                print(f"Attempting {method['name']} (attempt {attempt + 1})")
                
                # Run FFmpeg with timeout
                result = subprocess.run(
                    method['cmd'],
                    check=True,
                    timeout=method['timeout'],
                    capture_output=True,
                    text=True
                )
                
                # Validate output file
                if not os.path.exists(output_path):
                    raise ValueError("Output file not created")
                
                if os.path.getsize(output_path) < min_file_size:
                    raise ValueError(f"Output file too small (size: {os.path.getsize(output_path)} bytes)")
                
                # Verify video stream
                probe_cmd = [
                    ffmpeg_path if ffmpeg_path else 'ffprobe',
                    '-v', 'error',
                    '-select_streams', 'v:0',
                    '-show_entries', 'stream=codec_name,width,height,duration',
                    '-of', 'json',
                    output_path
                ]
                probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
                
                if probe_result.returncode != 0:
                    raise ValueError(f"FFprobe validation failed: {probe_result.stderr}")
                
                print(f"Success with {method['name']}")
                return True
                
            except subprocess.TimeoutExpired:
                print(f"{method['name']} timed out on attempt {attempt + 1}")
                if os.path.exists(output_path):
                    os.remove(output_path)
                if attempt == max_attempts - 1:
                    continue  # Try next method
                time.sleep(2 ** attempt)  # Exponential backoff
                
            except subprocess.CalledProcessError as e:
                print(f"{method['name']} failed on attempt {attempt + 1}: {e.stderr}")
                if os.path.exists(output_path):
                    os.remove(output_path)
                if attempt == max_attempts - 1:
                    continue  # Try next method
                time.sleep(2 ** attempt)  # Exponential backoff
                
            except Exception as e:
                print(f"Unexpected error with {method['name']}: {str(e)}")
                if os.path.exists(output_path):
                    os.remove(output_path)
                if attempt == max_attempts - 1:
                    continue  # Try next method
                time.sleep(2 ** attempt)  # Exponential backoff
    
    # If we get here, all methods failed
    error_msg = f"All FFmpeg processing methods failed for {input_path}"
    print(error_msg)
    raise Exception(error_msg)

def download_via_rapidapi(video_id, input_path):
    """Download video using RapidAPI"""
    try:
        api_url = f"https://ytstream-download-youtube-videos.p.rapidapi.com/dl?id={video_id}"
        headers = {
            'x-rapidapi-key': 'c58bbfe08bmsh4487c5af7cf106fp1fa8d9jsn95391a6d8a1f',
            'x-rapidapi-host': 'ytstream-download-youtube-videos.p.rapidapi.com'
        }
        
        response = requests.get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        result = response.json()
        
        adaptive_formats = result.get('adaptiveFormats', [])
        formats = result.get('formats', [])
        
        if (not adaptive_formats or not isinstance(adaptive_formats, list)) and (not formats or not isinstance(formats, list)):
            raise ValueError(f"No valid formats found via RapidAPI for video {video_id}")
        
        download_url = None
        for format_list in [formats, adaptive_formats]:
            for format_item in format_list:
                if format_item.get('url'):
                    download_url = format_item.get('url')
                    print(f"Using RapidAPI format: {format_item.get('qualityLabel', 'unknown quality')}")
                    break
            if download_url:
                break
        
        if not download_url:
            raise ValueError(f"No valid download URL found via RapidAPI for video {video_id}")

        print(f"Downloading video to path: {input_path}")
        os.makedirs(os.path.dirname(input_path), exist_ok=True)
        
        download_response = requests.get(download_url, timeout=90)
        download_response.raise_for_status()
        video_content = download_response.content
        
        if len(video_content) < 1024:
            raise ValueError("Downloaded file via RapidAPI is too small or empty")

        with open(input_path, 'wb') as f:
            f.write(video_content)
            
        return True
    except Exception as e:
        print(f"RapidAPI download failed: {str(e)}")
        return False

# Update your yt-dlp download function to properly use cookies
def download_via_ytdlp(video_id, input_path, use_cookies=False):
    """Enhanced video downloader with better error handling and fallbacks"""
    format_combinations = [
        'bv*[height<=720]+ba/b[height<=720]',
        'bv*+ba/b',
        'best[ext=mp4]',
        'worst[ext=mp4]',
        'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]'
    ]

    player_clients = [
        ['android', 'web'],
        ['tv_embedded', 'web'],
        ['android'],
        ['web'],
        ['embedded', 'web']
    ]

    url_variants = [
        f'https://www.youtube.com/watch?v={video_id}',
        f'https://youtu.be/{video_id}',
        f'https://www.youtube.com/embed/{video_id}',
        f'https://m.youtube.com/watch?v={video_id}'
    ]

    last_error = None
    attempt_count = 0

    # First try with cookies if available
    if use_cookies and os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 100:
        print("Attempting with cookies...")
        try:
            return _attempt_downloads(video_id, input_path, format_combinations, 
                                     player_clients, url_variants, True)
        except Exception as e:
            print(f"Cookie attempt failed: {e}")

    # Fallback to without cookies
    print("Falling back to no-cookie download...")
    try:
        return _attempt_downloads(video_id, input_path, format_combinations,
                                player_clients, url_variants, False)
    except Exception as e:
        raise Exception(f"All download attempts failed: {e}")

def _attempt_downloads(video_id, input_path, formats, clients, urls, use_cookies):
    """Helper function to attempt downloads with given parameters"""
    for format_str in formats:
        for client in clients:
            for url in urls:
                try:
                    ydl_opts = {
                        'format': format_str,
                        'outtmpl': input_path,
                        'quiet': True,
                        'no_warnings': True,
                        'retries': 3,
                        'fragment_retries': 3,
                        'extractor_retries': 3,
                        'ignoreerrors': False,
                        'noprogress': True,
                        'nooverwrites': True,
                        'continuedl': True,
                        'nopart': True,
                        'extractor_args': {
                            'youtube': {
                                'skip': ['dash', 'hls'],
                                'player_client': client,
                                'player_skip': ['config', 'webpage']
                            }
                        },
                        'cookiefile': COOKIES_FILE if use_cookies else None,
                        'http_headers': {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                            'Accept-Language': 'en-US,en;q=0.9',
                            'Referer': 'https://www.youtube.com/',
                            'Origin': 'https://www.youtube.com'
                        },
                        'geo_bypass': True,
                        'geo_bypass_country': 'US',
                        'concurrent_fragment_downloads': 3,
                        'buffersize': '16M',
                        'socket_timeout': 30,
                        'extract_flat': False
                    }

                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=True)
                        
                        # Add explicit error checking
                        if info is None:
                            raise Exception("Failed to extract video info - video may be unavailable")
                            
                        if info.get('availability') != 'public':
                            raise Exception(f"Video is not publicly available. Status: {info.get('availability')}")
                            
                        if 'entries' in info:  # Playlist
                            raise Exception("URL is a playlist, not a single video")
                            
                        return True
                        
                except yt_dlp.utils.DownloadError as e:
                    if "Video unavailable" in str(e):
                        # Get more specific error message
                        if "This video is private" in str(e):
                            raise Exception("Video is private - requires login")
                        elif "This video is not available" in str(e):
                            raise Exception("Video is geo-restricted or removed")
                        else:
                            raise Exception(f"Video unavailable: {str(e)}")
                    raise

    raise Exception("All download attempts failed")
    
    
def emergency_fallback_download(video_id, input_path, max_retries=3):
    """Enhanced last-resort download methods with multiple fallback strategies"""
    print("Attempting emergency fallback methods...")
    
    # Method 1: Proxy rotation with retries
    def try_proxy_services():
        proxy_services = [
            # Free tier proxy services (rotated)
            "https://www.youtubepp.com/download?id=",
            "https://yt1s.com/api/ajaxSearch/index?q=",
            "https://loader.to/ajax/download.php?url=",
            # Premium proxy services (configure these in environment variables)
            os.getenv('PREMIUM_PROXY_1', ''),
            os.getenv('PREMIUM_PROXY_2', '')
        ]
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.5'
        }
        
        for attempt in range(max_retries):
            for proxy_url in proxy_services:
                if not proxy_url:
                    continue
                    
                try:
                    full_url = f"{proxy_url}{video_id}" if "=" in proxy_url else f"{proxy_url}https://youtube.com/watch?v={video_id}"
                    print(f"Attempt {attempt+1} with proxy: {full_url}")
                    
                    response = requests.get(
                        full_url,
                        headers=headers,
                        timeout=30,
                        allow_redirects=True
                    )
                    
                    if response.status_code == 200:
                        # Handle different response types
                        if 'json' in response.headers.get('Content-Type', ''):
                            data = response.json()
                            if 'durl' in data:  # Some services return JSON with actual URL
                                dl_url = data['durl']
                                response = requests.get(dl_url, stream=True)
                        
                        with open(input_path, 'wb') as f:
                            if hasattr(response, 'raw'):
                                shutil.copyfileobj(response.raw, f)
                            else:
                                f.write(response.content)
                        
                        if os.path.getsize(input_path) > 1024:
                            return True
                except Exception as e:
                    print(f"Proxy attempt failed: {str(e)}")
                    continue
            
            if attempt < max_retries - 1:
                wait_time = 2 ** (attempt + 1)
                print(f"Waiting {wait_time} seconds before next proxy attempt...")
                time.sleep(wait_time)
        
        return False

    # Method 2: Alternative extractors with format fallbacks
    def try_alternative_extractors():
        format_priorities = [
            'worst[ext=mp4]',  # Smallest file size
            'bestvideo[height<=480]+bestaudio/best[height<=480]',
            'bestvideo+bestaudio/best',
            'mp4'  # Generic format
        ]
        
        extractor_args = [
            {'youtube': {'skip': [], 'player_client': ['android']}},
            {'youtube': {'skip': [], 'player_client': ['web']}},
            {'youtube': {'skip': ['dash'], 'player_skip': ['webpage']}},
            {}  # No special args
        ]
        
        for attempt in range(max_retries):
            for fmt in format_priorities:
                for ext_args in extractor_args:
                    try:
                        ydl_opts = {
                            'format': fmt,
                            'outtmpl': input_path,
                            'quiet': True,
                            'no_warnings': True,
                            'force_generic_extractor': True,
                            'extractor_args': ext_args,
                            'http_headers': {
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                                'Accept-Language': 'en-US,en;q=0.9'
                            }
                        }
                        
                        # Add proxy if available
                        if os.getenv('YT_DL_PROXY'):
                            ydl_opts['proxy'] = os.getenv('YT_DL_PROXY')
                        
                        print(f"Attempt {attempt+1} with format {fmt} and args {ext_args}")
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([f'https://youtu.be/{video_id}'])
                        
                        if os.path.exists(input_path) and os.path.getsize(input_path) > 1024:
                            return True
                    except Exception as e:
                        print(f"Extractor attempt failed: {str(e)}")
                        if os.path.exists(input_path):
                            os.remove(input_path)
                        continue
            
            if attempt < max_retries - 1:
                time.sleep(5)  # Wait before next attempt
        
        return False

    # Method 3: Direct download with multiple approaches
    def try_direct_download():
        download_methods = [
            # wget with different user agents
            lambda: subprocess.run([
                'wget', '-O', input_path,
                '-U', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                f'https://www.youtube.com/watch?v={video_id}'
            ], check=True, timeout=60),
            
            # curl approach
            lambda: subprocess.run([
                'curl', '-L', '-o', input_path,
                '-A', 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)',
                f'https://www.youtube.com/watch?v={video_id}'
            ], check=True, timeout=60),
            
            # aria2c for resumable downloads
            lambda: subprocess.run([
                'aria2c', '-o', input_path,
                f'https://www.youtube.com/watch?v={video_id}'
            ], check=True, timeout=60) if shutil.which('aria2c') else None
        ]
        
        for method in download_methods:
            try:
                if method:
                    method()
                    if os.path.exists(input_path) and os.path.getsize(input_path) > 1024:
                        return True
            except Exception as e:
                print(f"Direct download failed: {str(e)}")
                if os.path.exists(input_path):
                    os.remove(input_path)
                continue
        
        return False

    # Execute all methods with proper cleanup
    try:
        for method in [try_proxy_services, try_alternative_extractors, try_direct_download]:
            try:
                if method():
                    return True
            except Exception as e:
                print(f"Emergency method {method.__name__} failed: {str(e)}")
                continue
            
            # Cleanup between methods
            if os.path.exists(input_path):
                os.remove(input_path)
    finally:
        # Final verification
        if os.path.exists(input_path) and os.path.getsize(input_path) > 1024:
            return True
    
    return False


def download_video(video_id, output_path, max_retries=3):
    """
    Enhanced video download function with comprehensive retry logic and validation
    Attempts multiple download methods with proper error handling and validation
    
    Args:
        video_id (str): YouTube video ID
        output_path (str): Path to save the downloaded video
        max_retries (int): Maximum number of retry attempts (default: 3)
        
    Returns:
        bool: True if download succeeded, raises exception if all attempts fail
    """
    # Validate output directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    # Define download methods in order of preference
    download_methods = [
        # 1. Premium method with fresh cookies
        lambda: premium_download(video_id, output_path),
        # 2. yt-dlp with validated cookies
        lambda: download_via_ytdlp_with_cookies(video_id, output_path),
        # 3. Proxy download with rotating IPs
        lambda: proxy_download(video_id, output_path),
        # 4. Embedded cookies fallback
        lambda: embedded_cookies_download(video_id, output_path),
        # 5. RapidAPI fallback
        lambda: download_via_rapidapi(video_id, output_path),
        # 6. Emergency methods
        lambda: emergency_fallback_download(video_id, output_path)
    ]
    
    for attempt in range(max_retries):
        try:
            print(f"Download attempt {attempt + 1}/{max_retries} for video {video_id}")
            
            # Try each download method until one succeeds
            for method in download_methods:
                method_name = method.__name__ if hasattr(method, '__name__') else method.__class__.__name__
                print(f"Trying method: {method_name}")
                
                try:
                    if method():
                        # Validate the downloaded file
                        if validate_video_file(output_path):
                            print(f"Successfully downloaded video {video_id} using {method_name}")
                            return True
                        else:
                            print(f"Downloaded file failed validation, trying next method")
                            os.remove(output_path)
                            continue
                except Exception as e:
                    print(f"Method {method_name} failed: {str(e)}")
                    if os.path.exists(output_path):
                        os.remove(output_path)
                    continue
            
            # If we get here, all methods failed
            raise Exception("All download methods failed for this attempt")
            
        except Exception as e:
            print(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt == max_retries - 1:
                raise Exception(f"Failed to download video after {max_retries} attempts: {str(e)}")
            
            # Exponential backoff
            wait_time = min(2 ** (attempt + 1), 30)  # Cap at 30 seconds
            print(f"Waiting {wait_time} seconds before retry...")
            time.sleep(wait_time)
    
    raise Exception(f"All download attempts failed for video {video_id}")


def validate_video_file(file_path):
    """
    Thorough validation of downloaded video file
    
    Args:
        file_path (str): Path to the video file
        
    Returns:
        bool: True if file is valid, False otherwise
    """
    if not os.path.exists(file_path):
        print(f"File does not exist: {file_path}")
        return False
    
    file_size = os.path.getsize(file_path)
    if file_size < 1024:  # 1KB minimum
        print(f"File is too small ({file_size} bytes): {file_path}")
        return False
    
    # Check basic file type
    try:
        import magic
        file_type = magic.from_file(file_path)
        if 'MP4' not in file_type and 'ISO Media' not in file_type:
            print(f"Invalid file type: {file_type}")
            return False
    except ImportError:
        print("python-magic not available, skipping file type check")
    
    # Verify with ffprobe
    try:
        cmd = [
            ffmpeg_path if ffmpeg_path else 'ffprobe',
            '-v', 'error',
            '-show_format',
            '-show_streams',
            file_path
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            print(f"FFprobe validation failed: {result.stderr}")
            return False
        
        # Check for at least one video stream
        if 'codec_type=video' not in result.stdout:
            print("No video stream found in file")
            return False
            
        return True
        
    except Exception as e:
        print(f"Error during ffprobe validation: {str(e)}")
        return False


def premium_download(video_id, output_path):
    """
    Highest success rate download method using premium cookies
    """
    try:
        # Get fresh premium cookies (implement your own method)
        cookies_file = get_premium_cookies()
        if not cookies_file or not os.path.exists(cookies_file):
            return False
        
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'outtmpl': output_path,
            'cookiefile': cookies_file,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.youtube.com/',
                'Origin': 'https://www.youtube.com',
                'X-YouTube-Client-Name': '1',
                'X-YouTube-Client-Version': '2.20250101.00.00'
            },
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'web'],
                    'skip': ['hls', 'dash']
                }
            },
            'retries': 10,
            'fragment_retries': 10,
            'extractor_retries': 3,
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': False
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=True)
            if not info.get('requested_downloads'):
                raise Exception("No downloads were requested")
        
        return os.path.exists(output_path) and os.path.getsize(output_path) > 1024
        
    except Exception as e:
        print(f"Premium download failed: {str(e)}")
        if os.path.exists(output_path):
            os.remove(output_path)
        return False


def proxy_download(video_id, output_path):
    """Use rotating residential proxies to avoid detection"""
    try:
        proxies = get_fresh_proxies()  # Implement your proxy rotation
        
        ydl_opts = {
            'format': 'worst[ext=mp4]',  # Lower quality less likely to trigger blocks
            'outtmpl': output_path,
            'proxy': proxies[0]['url'],
            'http_headers': {
                'User-Agent': proxies[0]['user_agent'],
                'Accept-Language': 'en-US,en;q=0.5',
                'X-Forwarded-For': proxies[0]['ip']
            },
            'extractor_args': {
                'youtube': {
                    'player_client': ['tv_embedded', 'web']
                }
            },
            'retries': 5,
            'sleep_interval': 5,
            'max_sleep_interval': 30,
            'ignoreerrors': True
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
        
        return os.path.exists(output_path) and os.path.getsize(output_path) > 1024
        
    except Exception as e:
        print(f"Proxy download failed: {str(e)}")
        if os.path.exists(output_path):
            os.remove(output_path)
        return False


def embedded_cookies_download(video_id, output_path):
    """Use hardcoded cookies that get periodically updated"""
    cookies = """# Netscape HTTP Cookie File
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tCONSENT\tYES+cb.20250101-11-p0.en+FX+999
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tPREF\tf6=40000000&tz=UTC
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tVISITOR_INFO1_LIVE\tCg9JZ3FfV2hITE1jZw%3D%3D
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tYSC\tDGVN2JXJQFE
"""
    
    cookie_file = f'/tmp/yt_cookies_{video_id}.txt'
    try:
        with open(cookie_file, 'w') as f:
            f.write(cookies)
        
        ydl_opts = {
            'format': 'best[height<=480]',
            'outtmpl': output_path,
            'cookiefile': cookie_file,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0',
                'Accept-Language': 'en-US,en;q=0.9'
            },
            'retries': 3,
            'ignoreerrors': True
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
        
        return os.path.exists(output_path) and os.path.getsize(output_path) > 1024
        
    except Exception as e:
        print(f"Embedded cookies download failed: {str(e)}")
        if os.path.exists(output_path):
            os.remove(output_path)
        return False
    finally:
        try:
            os.remove(cookie_file)
        except:
            pass

def api_fallback_download(video_id, input_path):
    """Fallback to paid API services"""
    apis = [
        {
            'name': 'RapidAPI',
            'url': f'https://ytstream-download-youtube-videos.p.rapidapi.com/dl?id={video_id}',
            'headers': {
                'x-rapidapi-key': 'c58bbfe08bmsh4487c5af7cf106fp1fa8d9jsn95391a6d8a1f',
                'x-rapidapi-host': 'ytstream-download-youtube-videos.p.rapidapi.com'
            }
        },
        {
            'name': 'YouTubeDL API',
            'url': 'https://api.yt-dlp.org/download',
            'params': {
                'url': f'https://www.youtube.com/watch?v={video_id}',
                'format': 'mp4'
            }
        }
    ]
    
    for api in apis:
        try:
            response = requests.get(
                api['url'],
                headers=api.get('headers', {}),
                params=api.get('params', {}),
                timeout=30
            )
            response.raise_for_status()
            
            with open(input_path, 'wb') as f:
                f.write(response.content)
                
            if os.path.getsize(input_path) > 1024:
                return True
        except Exception as e:
            print(f"API {api['name']} failed: {str(e)}")
            continue
    
    return False

def get_premium_cookies():
    """Get fresh premium cookies from secure storage"""
    # Implement this to get cookies from:
    # - Encrypted S3 bucket
    # - Database
    # - Cookie generation service
    return '/path/to/fresh_cookies.txt'

def get_fresh_proxies():
    """Get rotating residential proxies"""
    # Implement proxy rotation from:
    # - Luminati
    # - Smartproxy
    # - Oxylabs
    return [
        {
            'url': 'http://user:pass@proxy1.com:8000',
            'ip': '192.168.1.1',
            'user_agent': 'Mozilla/5.0...'
        }
    ]
# New function for downloading with embedded cookies
def download_via_ytdlp_with_embedded_cookies(video_id, input_path):
    """Download video using yt-dlp with embedded cookies in the request headers"""
    # These are example cookies - you should replace them with valid ones
    # or implement a way to get fresh cookies dynamically
    embedded_cookies = {
        'CONSENT': 'YES+cb.20220301-11-p0.en+FX+910',
        'SOCS': 'CAISHAgCEhJnd3NfMjAyMzAzMDgtMF9SQzIaAmVuIAEaBgiA_LyuBg',
        'PREF': 'tz=UTC&f6=40000000',
        'YSC': 'DGVN2JXJQFE',
        'VISITOR_INFO1_LIVE': 'k35Esl4JdfA'
    }
    
    # Format cookies for headers
    cookie_header = '; '.join([f'{k}={v}' for k, v in embedded_cookies.items()])
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/mp4/best[height<=720]',
        'outtmpl': input_path,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.youtube.com/',
            'Origin': 'https://www.youtube.com',
            'Cookie': cookie_header
        },
        # Other options remain the same as in your original download_via_ytdlp
        'quiet': False,
        'no_warnings': False,
        'retries': 3,
        'fragment_retries': 3,
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
        },
        'extractor_args': {
            'youtube': {
                'skip': ['dash', 'hls'],
                'player_client': ['android', 'web']
            }
        },
        'throttled_rate': '1M',
        'sleep_interval': 2,
        'max_sleep_interval': 10,
        'force_ipv4': True,
        'geo_bypass': True,
        'geo_bypass_country': 'US',
        'extract_flat': False,
        'concurrent_fragment_downloads': 3,
        'buffersize': '16M',
        'no_check_certificate': True,
        'verbose': True
    }
    
    try:
        os.makedirs(os.path.dirname(input_path), exist_ok=True)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=True)
            
            if not info_dict.get('requested_downloads'):
                raise Exception("No downloads were requested")
        
        if not os.path.exists(input_path):
            raise Exception("Downloaded file not found")
            
        if os.path.getsize(input_path) < 1024:
            raise Exception("Downloaded file is too small (possibly incomplete)")
        
        print(f"Successfully downloaded video to: {input_path}")
        return True
        
    except Exception as e:
        print(f"yt-dlp download with embedded cookies failed: {str(e)}")
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except:
                pass
        return False

def download_fresh_cookies_file():
    """Download a fresh cookies file from a secure source"""
    try:
        # This could be from an internal API, encrypted S3 bucket, etc.
        # For security, you should implement this to get cookies from your secure source
        
        # Example implementation (replace with your actual secure source)
        cookies_url = "https://your-secure-api.example.com/get_youtube_cookies"
        response = requests.get(
            cookies_url,
            headers={'Authorization': f'Bearer {os.getenv("COOKIES_API_KEY")}'},
            timeout=30
        )
        response.raise_for_status()
        
        # Save the cookies file
        with open(COOKIES_FILE, 'wb') as f:
            f.write(response.content)
            
        # Validate the downloaded cookies
        if validate_cookies_file(COOKIES_FILE):
            print("Successfully downloaded fresh cookies file")
            return True
        else:
            print("Downloaded cookies file is invalid")
            return False
            
    except Exception as e:
        print(f"Error downloading fresh cookies: {str(e)}")
        return False

def download_via_ytdlp_without_cookies(video_id, input_path):
    """Download video using yt-dlp without cookies"""
    ydl_opts = {
        'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]',
        'outtmpl': input_path,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.youtube.com/',
            'Origin': 'https://www.youtube.com'
        },
        'retries': 3,
        'fragment_retries': 3,
        'extractor_retries': 3,
        'no_check_certificate': True,
        'ignoreerrors': False,
        'noprogress': True,
        'nooverwrites': False,
        'continuedl': False,
        'nopart': True,
        'windowsfilenames': sys.platform == 'win32',
        'paths': {
            'home': DOWNLOAD_DIR,
            'temp': TMP_DIR
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web']
            }
        },
        'throttled_rate': '1M',
        'sleep_interval': 2,
        'max_sleep_interval': 10,
        'force_ipv4': True,
        'geo_bypass': True,
        'geo_bypass_country': 'US',
        'extract_flat': False,
        'concurrent_fragment_downloads': 3,
        'buffersize': '16M'
    }
    
    try:
        os.makedirs(os.path.dirname(input_path), exist_ok=True)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=True)
            
            if not info_dict.get('requested_downloads'):
                raise Exception("No downloads were requested")
        
        if not os.path.exists(input_path):
            raise Exception("Downloaded file not found")
            
        if os.path.getsize(input_path) < 1024:
            raise Exception("Downloaded file is too small (possibly incomplete)")
        
        print(f"Successfully downloaded video to: {input_path}")
        return True
        
    except Exception as e:
        print(f"yt-dlp download without cookies failed: {str(e)}")
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except:
                pass
        return False




    
def download_video_with_retries(video_id, output_path, max_attempts=3):
    methods = [
        download_via_ytdlp_with_embedded_cookies,
        download_via_ytdlp_without_cookies,
        download_via_rapidapi,
        emergency_fallback_download
    ]
    
    for attempt in range(max_attempts):
        for method in methods:
            try:
                if method(video_id, output_path):
                    if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                        return True
                    os.remove(output_path)
            except Exception as e:
                print(f"Attempt {attempt+1} with {method.__name__} failed: {str(e)}")
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except:
                        pass
                continue
        
        if attempt < max_attempts - 1:
            wait_time = 2 ** (attempt + 1)
            print(f"Waiting {wait_time} seconds before retry...")
            time.sleep(wait_time)
    
    raise Exception(f"All download methods failed after {max_attempts} attempts")

def download_via_ytdlp_with_cookies(video_id, output_path):
    """Download using yt-dlp with cookies for higher success rate"""
    if not validate_and_refresh_cookies():
        print("Warning: No valid cookies available, falling back to without cookies")
        return download_via_ytdlp_without_cookies(video_id, output_path)
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]',
        'outtmpl': output_path,
        'cookiefile': COOKIES_FILE,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.youtube.com/',
            'Origin': 'https://www.youtube.com'
        },
        'retries': 3,
        'fragment_retries': 3,
        'extractor_retries': 3,
        'no_check_certificate': True
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
        
        # Validate download
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
            raise ValueError("Downloaded file is invalid or too small")
            
        return True
    except Exception as e:
        print(f"Download with cookies failed: {str(e)}")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except:
                pass
        return False


# Updated cookie validation function
def validate_and_refresh_cookies():
    """Ensure we have valid cookies and refresh them if needed"""
    # First check if we have a cookies file that might work
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 100:
        try:
            # Test the cookies with a simple request
            test_cmd = [
                sys.executable, "-m", "yt_dlp",
                "--cookies", COOKIES_FILE,
                "--skip-download",
                "--print", "%(title)s",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            ]
            result = subprocess.run(
                test_cmd,
                timeout=30,
                capture_output=True,
                text=True
            )
            
            if "Sign in to confirm you're not a bot" not in result.stderr:
                print("Existing cookies are still valid")
                return True
        except Exception as e:
            print(f"Error validating existing cookies: {str(e)}")
    
    print("No valid cookies found, attempting to generate new ones...")
    
    # Try different methods to get cookies
    cookie_sources = [
        # 1. Try to extract from any available browser
        lambda: generate_cookies_from_browser(),
        # 2. Try to use embedded cookies as fallback
        lambda: generate_embedded_cookies_file(),
        # 3. Try to download a fresh cookies file from a secure source
        lambda: download_fresh_cookies_file()
    ]
    
    for source in cookie_sources:
        try:
            if source():
                print("Successfully generated new cookies")
                return True
        except Exception as e:
            print(f"Cookie generation attempt failed: {str(e)}")
            continue
    
    print("All cookie generation methods failed")
    return False

def generate_cookies_from_browser():
    """Try to generate cookies from any available browser"""
    browsers = ['chrome', 'firefox', 'edge', 'brave']
    for browser in browsers:
        try:
            print(f"Attempting to generate cookies from {browser}...")
            cmd = [
                sys.executable, "-m", "yt_dlp",
                "--cookies-from-browser", browser,
                "--cookies", COOKIES_FILE,
                "--skip-download",
                "--no-check-certificate",
                "https://www.youtube.com"
            ]
            result = subprocess.run(
                cmd, 
                check=True, 
                timeout=60,
                capture_output=True,
                text=True
            )
            
            if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 100:
                print(f"Successfully generated cookies from {browser}")
                return True
        except subprocess.TimeoutExpired:
            print(f"Timeout generating cookies from {browser}")
            continue
        except Exception as e:
            print(f"Error generating cookies from {browser}: {str(e)}")
            continue
    return False

def generate_embedded_cookies_file():
    """Create a cookies file with embedded cookies that might work"""
    embedded_cookies = """# Netscape HTTP Cookie File
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tCONSENT\tYES+cb.20220301-11-p0.en+FX+910
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tPREF\tf6=40000000&tz=UTC
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tVISITOR_INFO1_LIVE\tCg9JZ3FfV2hITE1jZw%3D%3D
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tYSC\tDGVN2JXJQFE
"""
    try:
        with open(COOKIES_FILE, 'w', encoding='utf-8') as f:
            f.write(embedded_cookies)
        return True
    except Exception as e:
        print(f"Error writing embedded cookies: {str(e)}")
        return False

def download_via_pytube(video_id, input_path):
    """Fallback download method using pytube"""
    try:
        from pytube import YouTube
        
        yt = YouTube(f'https://www.youtube.com/watch?v={video_id}')
        stream = yt.streams.filter(
            progressive=True,
            file_extension='mp4',
            resolution='720p'
        ).first()
        
        if not stream:
            stream = yt.streams.filter(
                progressive=True,
                file_extension='mp4'
            ).order_by('resolution').desc().first()
        
        if stream:
            os.makedirs(os.path.dirname(input_path), exist_ok=True)
            stream.download(output_path=os.path.dirname(input_path), filename=os.path.basename(input_path))
            return True
            
        return False
    except Exception as e:
        print(f"Pytube download failed: {str(e)}")
        return False
def validate_and_refresh_cookies():
    """Enhanced cookie validation with better error handling and refresh logic"""
    try:
        # First check if we have a cookies file that might work
        if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 100:
            # Test cookies with a simple YouTube request
            test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            test_cmd = [
                sys.executable, "-m", "yt_dlp",
                "--cookies", COOKIES_FILE,
                "--skip-download",
                "--print", "%(title)s",
                test_url
            ]
            
            try:
                result = subprocess.run(
                    test_cmd,
                    timeout=30,
                    capture_output=True,
                    text=True
                )
                
                if "Sign in" not in result.stderr and result.returncode == 0:
                    print("Existing cookies are still valid")
                    return True
            except subprocess.TimeoutExpired:
                print("Cookie validation timed out")
            except Exception as e:
                print(f"Cookie validation error: {str(e)}")
        
        # If we get here, cookies are invalid - generate fresh ones
        print("Generating fresh cookies...")
        browsers = ['chrome', 'firefox', 'edge', 'brave', 'safari']
        
        for browser in browsers:
            try:
                print(f"Attempting to generate cookies from {browser}...")
                cmd = [
                    sys.executable, "-m", "yt_dlp",
                    "--cookies-from-browser", browser,
                    "--cookies", COOKIES_FILE,
                    "--skip-download",
                    "--no-check-certificate",
                    "https://www.youtube.com"
                ]
                
                # Set longer timeout for cookie generation
                result = subprocess.run(
                    cmd, 
                    timeout=120,  # 2 minutes timeout
                    capture_output=True,
                    text=True
                )
                
                # Verify the generated cookies file
                if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 100:
                    # Quick validation of the cookies format
                    with open(COOKIES_FILE, 'r') as f:
                        first_line = f.readline().strip()
                        if first_line.startswith(('# HTTP Cookie File', '# Netscape HTTP Cookie File')):
                            print(f"Successfully generated cookies from {browser}")
                            return True
                
                print(f"Failed to generate valid cookies from {browser}")
                if result.stderr:
                    print("Error output:", result.stderr)
                
            except subprocess.TimeoutExpired:
                print(f"Timeout generating cookies from {browser}")
                continue
            except Exception as e:
                print(f"Error generating cookies from {browser}: {str(e)}")
                continue
        
        # Final fallback - try to use embedded cookies if all else fails
        print("Attempting to use embedded cookies as fallback...")
        embedded_cookies = """# Netscape HTTP Cookie File
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tCONSENT\tYES+cb.20220301-11-p0.en+FX+910
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tPREF\tf6=40000000&tz=UTC
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tVISITOR_INFO1_LIVE\tk35Esl4JdfA
.youtube.com\tTRUE\t/\tTRUE\t2147483647\tYSC\tDGVN2JXJQFE
"""
        try:
            with open(COOKIES_FILE, 'w') as f:
                f.write(embedded_cookies)
            
            # Test if these cookies work
            test_result = subprocess.run(
                test_cmd,
                timeout=30,
                capture_output=True,
                text=True
            )
            
            if "Sign in" not in test_result.stderr and test_result.returncode == 0:
                print("Fallback embedded cookies worked")
                return True
            
        except Exception as e:
            print(f"Failed to use embedded cookies: {str(e)}")
        
        return False
    
    except Exception as e:
        print(f"Unexpected error in cookie validation: {str(e)}")
        return False
    
    
ERROR_SUGGESTIONS = {
    'authentication_error': [
        "Upload fresh YouTube cookies",
        "Try again later if rate-limited",
        "Use a different network/VPN"
    ],
    'processing_error': [
        "Try shorter clip durations",
        "Check video formats are supported",
        "Verify sufficient system resources"
    ],
    'storage_error': [
        "Check your S3 bucket permissions",
        "Verify your AWS credentials",
        "Try again later if service is overloaded"
    ],
    'resource_error': [
        "Free up disk space",
        "Check available memory",
        "Reduce the number of concurrent operations"
    ],
    'unexpected_error': [
        "Try again with different clips",
        "Check server logs for details",
        "Contact support if problem persists"
    ]
}

def classify_error(error):
    """Classify errors for better user feedback"""
    error_str = str(error).lower()
    if 'cookies' in error_str or 'sign in' in error_str or 'bot' in error_str:
        return 'authentication_error'
    elif 'ffmpeg' in error_str or 'moov atom' in error_str or 'processing' in error_str:
        return 'processing_error'
    elif 's3' in error_str or 'upload' in error_str or 'aws' in error_str:
        return 'storage_error'
    elif 'disk' in error_str or 'space' in error_str or 'memory' in error_str:
        return 'resource_error'
    return 'unexpected_error'

def get_error_suggestions(error_type):
    """Get suggestions for specific error types"""
    return ERROR_SUGGESTIONS.get(error_type, ERROR_SUGGESTIONS['unexpected_error'])

def has_sufficient_disk_space(min_free_gb=5):
    """Check if there's sufficient disk space available"""
    try:
        if sys.platform == 'win32':
            import ctypes
            free_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(os.path.splitdrive(DOWNLOAD_DIR)[0]), 
                None, None, ctypes.pointer(free_bytes)
            )
            free_gb = free_bytes.value / (1024**3)
        else:
            stat = shutil.disk_usage(DOWNLOAD_DIR)
            free_gb = stat.free / (1024**3)
        
        return free_gb >= min_free_gb
    except Exception as e:
        logger.error(f"Error checking disk space: {str(e)}")
        return False
    
def check_video_availability(video_id):
    """Check if a video is available before attempting download"""
    try:
        ydl_opts = {
            'skip_download': True,
            'quiet': True,
            'ignoreerrors': False
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
            
            if not info:
                return False, "No video information available"
                
            availability = info.get('availability')
            if availability != 'public':
                return False, f"Video is {availability}"
                
            return True, "Video is available"
            
    except Exception as e:
        return False, str(e)

    
@app.route('/merge-clips', methods=['POST'])
@limiter.limit("5 per minute")  # Add rate limiting
def merge_clips_route():
    try:
        data = request.get_json()
        clips = data.get('clips', [])
        
        # First check all videos are available
        for clip in clips:
            video_id = clip.get('videoId')
            available, reason = check_video_availability(video_id)
            if not available:
                return jsonify({
                    'error': f'Video {video_id} is not available: {reason}',
                    'status': False
                }), 400
        # Initial system checks
        health_checks = {
            'ffmpeg_available': ffmpeg_available,
            'disk_space': has_sufficient_disk_space(),
            'cookies_valid': validate_and_refresh_cookies(),
            's3_accessible': test_s3_connection()
        }

        if not all(health_checks.values()):
            failed_checks = {k: v for k, v in health_checks.items() if not v}
            return jsonify({
                'error': 'System health check failed',
                'failed_checks': failed_checks,
                'status': False
            }), 500

        # Request validation
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

        # Enhanced clip validation
        validation_errors = []
        max_clip_duration = 600  # 10 minutes
        max_total_duration = 3600  # 60 minutes
        total_duration = 0
        
        for i, clip in enumerate(clips):
            try:
                if not isinstance(clip, dict):
                    validation_errors.append(f"Clip {i}: Invalid format - expected dictionary")
                    continue
                    
                video_id = clip.get('videoId')
                if not video_id or not isinstance(video_id, str):
                    validation_errors.append(f"Clip {i}: Missing or invalid videoId")
                    continue
                
                try:
                    start_time = float(clip.get('startTime', 0))
                    end_time = float(clip.get('endTime', 0))
                    
                    if end_time <= start_time:
                        validation_errors.append(f"Clip {i}: Invalid time range (start_time >= end_time)")
                        continue
                        
                    clip_duration = end_time - start_time
                    if clip_duration > max_clip_duration:
                        validation_errors.append(f"Clip {i}: Duration too long (max {max_clip_duration}s)")
                        continue
                        
                    total_duration += clip_duration
                    
                except ValueError:
                    validation_errors.append(f"Clip {i}: Invalid startTime or endTime - must be numbers")
                    continue
                    
            except Exception as e:
                validation_errors.append(f"Clip {i}: Validation error - {str(e)}")
                continue

        if validation_errors:
            return jsonify({
                'error': 'Clip validation failed',
                'details': validation_errors,
                'status': False
            }), 400
            
        if total_duration > max_total_duration:
            return jsonify({
                'error': f'Total duration too long (max {max_total_duration}s)',
                'status': False
            }), 400

        # Setup working files
        timestamp = int(time.time())
        file_list_path = os.path.join(TMP_DIR, f'filelist_{timestamp}.txt')
        output_path = os.path.join(TMP_DIR, f'merged_clips_{timestamp}.mp4')
        processed_clips = []
        
        try:
            # Process each clip with enhanced error handling
            for clip_idx, clip in enumerate(clips):
                video_id = clip['videoId']
                start_time = float(clip.get('startTime', 0))
                end_time = float(clip.get('endTime', 0))
                
                input_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
                clip_output = os.path.join(TMP_DIR, f'clip_{video_id}_{int(start_time)}_{int(end_time)}.mp4')
                
                # Download and process with retries
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        logger.info(f"Processing clip {clip_idx+1}/{len(clips)} (attempt {attempt+1}) - {video_id}")
                        
                        # Download with validation
                        if not os.path.exists(input_path) or not validate_video_file(input_path):
                            logger.info(f"Downloading video {video_id}")
                            if not download_video_with_retries(video_id, input_path):
                                raise Exception(f"Failed to download video {video_id}")
                        
                        # Process clip with multiple FFmpeg fallbacks
                        if not safe_ffmpeg_process(input_path, clip_output, start_time, end_time):
                            raise Exception(f"Failed to process clip {video_id}")
                        
                        # Validate output
                        if not validate_video_file(clip_output):
                            raise Exception(f"Invalid output clip: {clip_output}")
                            
                        # Verify duration
                        clip_info = ffmpeg.probe(clip_output)
                        actual_duration = float(clip_info['format']['duration'])
                        expected_duration = end_time - start_time
                        
                        if abs(actual_duration - expected_duration) > 2.0:
                            logger.warning(f"Duration mismatch for {video_id}: expected {expected_duration}s, got {actual_duration}s")
                        
                        processed_clips.append({
                            'path': clip_output,
                            'info': clip,
                            'duration': actual_duration
                        })
                        break
                        
                    except Exception as e:
                        logger.error(f"Attempt {attempt+1} failed for clip {video_id}: {str(e)}")
                        if attempt == max_retries - 1:
                            raise Exception(f"Failed to process clip {video_id} after {max_retries} attempts: {str(e)}")
                        time.sleep(2 ** (attempt + 1))  # Exponential backoff
                        continue

            if not processed_clips:
                raise ValueError("No clips were successfully processed")

            # Create file list for concatenation
            with open(file_list_path, 'w', encoding='utf-8') as f:
                for clip in processed_clips:
                    f.write(f"file '{clip['path']}'\n")

            time.sleep(1)  # Allow file handles to release

            # Enhanced merge with multiple methods
            merge_success = False
            merge_errors = []
            
            for method in ['copy', 'encode', 'resilient']:
                try:
                    cmd = build_ffmpeg_merge_command(file_list_path, output_path, method)
                    logger.info(f"Attempting merge with method: {method}")
                    
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=300  # 5 minute timeout
                    )
                    
                    if result.returncode != 0:
                        raise Exception(result.stderr)
                    
                    # Validate merged output
                    if not validate_video_file(output_path):
                        raise Exception("Merged file validation failed")
                    
                    merge_success = True
                    break
                    
                except Exception as e:
                    merge_errors.append(f"{method} method failed: {str(e)}")
                    logger.error(f"Merge attempt failed: {str(e)}")
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except:
                            pass
                    continue
            
            if not merge_success:
                raise Exception(f"All merge methods failed:\n" + "\n".join(merge_errors))
            
            # Upload to S3 with retries and progress tracking
            unique_filename = f"merged_{uuid.uuid4()}.mp4"
            s3_url = upload_to_s3_with_retries(output_path, unique_filename)
            
            # Return success response
            return jsonify({
                'message': 'Clips merged successfully',
                's3Url': s3_url,
                'clipsInfo': [clip['info'] for clip in processed_clips],
                'success': True,
                'status': True,
                'fileNames3': unique_filename,
                'fileSize': os.path.getsize(output_path),
                'duration': sum(clip['duration'] for clip in processed_clips),
                'metadata': {
                    'clipCount': len(processed_clips),
                    'processingTime': time.time() - timestamp,
                    'format': get_video_metadata(output_path)
                }
            })

        except Exception as e:
            error_type = classify_error(e)
            logger.error(f"Error processing merge request: {str(e)}", exc_info=True)
            
            return jsonify({
                'error': str(e),
                'status': False,
                'type': error_type,
                'suggestions': get_error_suggestions(error_type),
                'traceId': str(uuid.uuid4())  # For support correlation
            }), 500
            
        finally:
            # Comprehensive cleanup
            cleanup_files = [
                file_list_path,
                output_path,
                *[clip['path'] for clip in processed_clips]
            ]
            
            cleanup_resources(cleanup_files)

    except Exception as e:
        logger.critical(f"Unhandled exception in merge-clips: {str(e)}", exc_info=True)
        return jsonify({
            'error': 'Internal server error',
            'status': False,
            'type': 'unexpected_error',
            'traceId': str(uuid.uuid4()),
            'suggestions': get_error_suggestions('unexpected_error')
        }), 500

# Helper functions used in the route
def build_ffmpeg_merge_command(file_list_path, output_path, method='copy'):
    """Build appropriate FFmpeg command based on merge method"""
    base_cmd = [
        ffmpeg_path if ffmpeg_path else 'ffmpeg',
        '-f', 'concat',
        '-safe', '0',
        '-i', file_list_path
    ]
    
    if method == 'copy':
        return base_cmd + [
            '-c', 'copy',
            '-movflags', 'faststart',
            '-y', output_path
        ]
    elif method == 'encode':
        return base_cmd + [
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', 'faststart',
            '-y', output_path
        ]
    else:  # resilient
        return base_cmd + [
            '-err_detect', 'ignore_err',
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', 'faststart',
            '-y', output_path
        ]

def upload_to_s3_with_retries(file_path, object_name, max_attempts=3):
    """Upload file to S3 with retries and progress tracking"""
    for attempt in range(max_attempts):
        try:
            logger.info(f"Uploading to S3 (attempt {attempt+1})")
            
            # Use multipart upload for large files
            config = None
            if os.path.getsize(file_path) > 100 * 1024 * 1024:  # 100MB
                config = boto3.s3.transfer.TransferConfig(
                    multipart_threshold=100 * 1024 * 1024,
                    max_concurrency=10,
                    multipart_chunksize=50 * 1024 * 1024,
                    use_threads=True
                )
            
            s3_client.upload_file(
                file_path,
                AWS_S3_BUCKET,
                object_name,
                ExtraArgs={
                    'ContentType': 'video/mp4',
                    'ACL': 'public-read'
                },
                Config=config
            )
            
            # Verify upload
            head = s3_client.head_object(
                Bucket=AWS_S3_BUCKET,
                Key=object_name
            )
            if head.get('ContentLength', 0) != os.path.getsize(file_path):
                raise Exception("Uploaded file size mismatch")
            
            # Generate presigned URL
            return s3_client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': AWS_S3_BUCKET,
                    'Key': object_name
                },
                ExpiresIn=604800  # 7 days
            )
            
        except Exception as e:
            logger.error(f"Upload attempt {attempt+1} failed: {str(e)}")
            if attempt == max_attempts - 1:
                raise Exception(f"Failed to upload to S3 after {max_attempts} attempts: {str(e)}")
            time.sleep(5 * (attempt + 1))
            continue

def cleanup_resources(file_paths):
    """Clean up temporary files with error handling"""
    for file_path in file_paths:
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Cleaned up: {file_path}")
        except Exception as e:
            logger.warning(f"Failed to cleanup {file_path}: {str(e)}")

def get_video_metadata(file_path):
    """Get video metadata using ffprobe"""
    try:
        probe = ffmpeg.probe(file_path)
        return {
            'format': probe['format']['format_name'],
            'duration': float(probe['format']['duration']),
            'size': os.path.getsize(file_path),
            'streams': [
                {
                    'codec_type': stream['codec_type'],
                    'codec_name': stream['codec_name'],
                    'width': stream.get('width'),
                    'height': stream.get('height'),
                    'bit_rate': stream.get('bit_rate')
                }
                for stream in probe['streams']
            ]
        }
    except Exception as e:
        logger.warning(f"Failed to get video metadata: {str(e)}")
        return None

def get_error_suggestions(error_type):
    """Return helpful suggestions based on error type"""
    suggestions = {
        'authentication_error': [
            "Try re-authenticating with YouTube by uploading fresh cookies",
            "Wait a while before retrying as you may be rate-limited",
            "Try using a different network/VPN if available"
        ],
        'processing_error': [
            "Try again with shorter clips",
            "Check that the input videos are in a supported format",
            "Verify there's enough disk space available"
        ],
        'storage_error': [
            "Check your S3 bucket permissions and configuration",
            "Verify your AWS credentials are valid",
            "Try again later if the storage service is overloaded"
        ],
        'unexpected_error': [
            "Try again with different clips",
            "Check server logs for more detailed error information",
            "Contact support if the problem persists"
        ]
    }
    return suggestions.get(error_type, ["An unknown error occurred"])
        
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8000)))