from flask import Flask, request, jsonify
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
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
from pytube import YouTube  
import random
import logging
import sieve
import urllib3
print(f"Using urllib3 version: {urllib3.__version__}")
import types



load_dotenv()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
COOKIES_FILE = os.path.join(BASE_DIR, 'cookies.txt')
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

PROXY_LIST = [
    "38.154.227.167:5868:pzvokxqt:v17333r03zxw",
    "92.113.242.158:6742:pzvokxqt:v17333r03zxw",
    "23.95.150.145:6114:pzvokxqt:v17333r03zxw",
    "198.23.239.134:6540:pzvokxqt:v17333r03zxw",
    "207.244.217.165:6712:pzvokxqt:v17333r03zxw",
    "107.172.163.27:6543:pzvokxqt:v17333r03zxw",
    "216.10.27.159:6837:pzvokxqt:v17333r03zxw",
    "136.0.207.84:6661:pzvokxqt:v17333r03zxw",
    "142.147.128.93:6593:pzvokxqt:v17333r03zxw",
    "206.41.172.74:6634:pzvokxqt:v17333r03zxw"
]

def get_random_proxy():
    proxy = random.choice(PROXY_LIST)
    host, port, user, pwd = proxy.split(":")
    proxy_url = f"http://{user}:{pwd}@{host}:{port}"
    return proxy_url

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
        "origins": ["https://clip-frontend-three.vercel.app"],  # Explicitly allow your frontend
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
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
            'x-rapidapi-key': '6820d4d822msh502bdc3b993dbd2p1a24c6jsndfbf9f3bc90b',
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
    
@app.route('/url/transcript', methods=['GET', 'POST'], strict_slashes=False)
def get_transcript_by_url():
    logger.info("Received request for /url/transcript")
    logger.info(f"Method: {request.method}, Headers: {request.headers}, Body: {request.get_data(as_text=True)}")

    if request.method == 'POST':
        if not request.is_json:
            logger.error("Request is not JSON")
            return jsonify({
                'message': "Request must be JSON",
                'status': False
            }), 400
        data = request.get_json()
        video_url = data.get('video_url') if data else None
        if not video_url:
            logger.error("No video_url in request body")
            return jsonify({
                'message': "Video URL is required in request body",
                'status': False
            }), 400
    else:  # GET
        video_url = request.args.get('url')
        if not video_url:
            logger.error("No video_url in query parameters")
            return jsonify({
                'message': "Video URL is required in query parameters",
                'status': False
            }), 400

    rapidapi_key = os.getenv('RAPIDAPI_KEY')
    if not rapidapi_key:
        logger.error("RapidAPI key is not configured")
        return jsonify({
            'message': "Server configuration error",
            'status': False
        }), 500

    api_url = "https://video-transcript-scraper.p.rapidapi.com/"
    payload = {"video_url": video_url}
    headers = {
        'x-rapidapi-key': rapidapi_key,
        'x-rapidapi-host': "video-transcript-scraper.p.rapidapi.com",
        'Content-Type': "application/json"
    }

    try:
        response = requests.post(api_url, json=payload, headers=headers)
        logger.info(f"API response status: {response.status_code}, body: {response.text}")
        response.raise_for_status()

        data = response.json()
        processed_transcript = []
        if 'transcripts' in data:
            for index, item in enumerate(data['transcripts']):
                if 'text' in item:
                    segment = {
                        'id': index + 1,
                        'text': item.get('text', '').strip(),
                        'startTime': item.get('start', None),
                        'endTime': None,
                        'duration': item.get('duration', None)
                    }
                    if segment['startTime'] is not None and segment['duration'] is not None:
                        segment['endTime'] = segment['startTime'] + segment['duration']
                    if segment['text']:
                        processed_transcript.append(segment)

        if not processed_transcript:
            logger.info("No transcript found for this video")
            return jsonify({
                'message': "No transcript found for this video",
                'status': False
            }), 404

        logger.info(f"Processed {len(processed_transcript)} segments")
        return jsonify({
            'message': "Transcript fetched successfully",
            'data': processed_transcript,
            'status': True,
            'totalSegments': len(processed_transcript)
        }), 200

    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error fetching transcript: {str(e)}, response: {e.response.text}")
        return jsonify({
            'message': f"Failed to fetch transcript: {str(e)}",
            'status': False
        }), e.response.status_code

    except requests.exceptions.JSONDecodeError:
        logger.error(f"Failed to parse API response as JSON: {response.text}")
        return jsonify({
            'message': "Invalid API response format",
            'status': False
        }), 500

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({
            'message': "An unexpected error occurred while fetching the transcript",
            'status': False
        }), 500
    
    

@app.route('/transcript/<video_id>', methods=['GET', 'POST'])
def get_transcript(video_id):
    if not video_id:
        return jsonify({
            'message': "Video ID is required",
            'status': False
        }), 400

    logger.info(f"Fetching transcript for video_id: {video_id} using scrapingdog API")

    api_key = "6865405067725052ca756102"  # TODO: Replace with os.getenv('SCRAPINGDOG_API_KEY')
    url = "https://api.scrapingdog.com/youtube/transcripts/"
    params = {
        "api_key": api_key,
        "v": video_id
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()  # Raises an exception for 4xx/5xx status codes

        # Check if the response is empty
        if not response.text.strip():
            logger.info("API returned an empty response")
            return jsonify({
                'message': "No transcript found for this video",
                'status': False
            }), 404

        # Attempt to parse the response as JSON
        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            logger.error(f"Failed to parse API response as JSON: {response.text}")
            return jsonify({
                'message': "Invalid API response format",
                'status': False
            }), 500

        # Process the JSON data based on the expected format with 'transcripts' key
        processed_transcript = []
        if isinstance(data, dict) and 'transcripts' in data:
            for index, item in enumerate(data['transcripts']):
                if isinstance(item, dict) and 'text' in item:
                    segment = {
                        'id': index + 1,
                        'text': item.get('text', '').strip(),
                        'startTime': item.get('start', None),
                        'endTime': None,  # Calculate endTime if needed
                        'duration': item.get('duration', None)
                    }
                    # Optionally calculate endTime if start and duration are provided
                    if segment['startTime'] is not None and segment['duration'] is not None:
                        segment['endTime'] = segment['startTime'] + segment['duration']
                    if segment['text']:
                        processed_transcript.append(segment)
        else:
            logger.error(f"Unexpected API response format: {type(data)}")
            return jsonify({
                'message': "Unexpected API response format",
                'status': False
            }), 500

        if not processed_transcript:
            logger.info("No valid transcript segments found")
            return jsonify({
                'message': "No valid transcript segments found",
                'status': False
            }), 404

        logger.info(f"Processed {len(processed_transcript)} segments")
        return jsonify({
            'message': "Transcript fetched successfully",
            'data': processed_transcript,
            'status': True,
            'totalSegments': len(processed_transcript)
        }), 200

    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error fetching transcript: {str(e)}")
        return jsonify({
            'message': f"Failed to fetch transcript: {str(e)}",
            'status': False
        }), e.response.status_code

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({
            'message': "An unexpected error occurred while fetching the transcript",
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
    """
    Generate a cookies file from the user's browser.
    Query parameters:
    - browser: The browser to extract cookies from (chrome, firefox, edge, etc.)
    - custom_path: Optional path to browser profile
    """
    try:
        browser = request.args.get('browser', 'chrome')
        custom_path = request.args.get('custom_path', None)
        
        cookies_file = os.path.join(BASE_DIR, 'youtube_cookies.txt')
        
        # First check if we have a custom browser path saved
        browser_config_file = os.path.join(BASE_DIR, 'browser_paths.json')
        if os.path.exists(browser_config_file) and not custom_path:
            try:
                with open(browser_config_file, 'r') as f:
                    browser_paths = json.load(f)
                    if browser in browser_paths and os.path.exists(browser_paths[browser]):
                        custom_path = browser_paths[browser]
                        print(f"Using saved browser path for {browser}: {custom_path}")
            except Exception as e:
                print(f"Error loading browser paths: {str(e)}")
                # Continue without saved paths
        
        # Define platform-specific browser profile paths
        platform_paths = {
            'win32': {
                'chrome': os.path.expanduser('~\\AppData\\Local\\Google\\Chrome\\User Data'),
                'firefox': os.path.expanduser('~\\AppData\\Roaming\\Mozilla\\Firefox\\Profiles'),
                'edge': os.path.expanduser('~\\AppData\\Local\\Microsoft\\Edge\\User Data'),
                'brave': os.path.expanduser('~\\AppData\\Local\\BraveSoftware\\Brave-Browser\\User Data'),
            },
            'linux': {
                'chrome': os.path.expanduser('~/.config/google-chrome'),
                'chrome-flatpak': os.path.expanduser('~/.var/app/com.google.Chrome/config/google-chrome'),
                'firefox': os.path.expanduser('~/.mozilla/firefox'),
                'brave': os.path.expanduser('~/.config/BraveSoftware/Brave-Browser'),
            },
            'darwin': {  # macOS
                'chrome': os.path.expanduser('~/Library/Application Support/Google/Chrome'),
                'firefox': os.path.expanduser('~/Library/Application Support/Firefox/Profiles'),
                'safari': os.path.expanduser('~/Library/Safari'),
                'brave': os.path.expanduser('~/Library/Application Support/BraveSoftware/Brave-Browser'),
            }
        }
        
        # If custom_path is not provided but we have a default for this platform/browser
        if not custom_path and sys.platform in platform_paths:
            # Check if it's a special case like chrome-flatpak
            if browser in platform_paths[sys.platform]:
                default_path = platform_paths[sys.platform][browser]
                print(f"Using default {browser} profile path for {sys.platform}: {default_path}")
                
                # Only use the default path if it exists
                if os.path.exists(default_path):
                    custom_path = default_path
                    print(f"Default path exists, will use it")
                else:
                    print(f"Default path doesn't exist, continuing without it")
        
        # Construct the command
        extract_cmd = [
            sys.executable, "-m", "yt_dlp", 
            "--cookies-from-browser"
        ]
        
        # Add browser name and optional path
        if custom_path:
            extract_cmd.append(f"{browser}:{custom_path}")
        else:
            extract_cmd.append(browser)
            
        # Add remaining arguments
        extract_cmd.extend([
            "--cookies", cookies_file,
            "-f", "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/mp4/best[height<=720]",
            "--print", "requested_downloads",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # Use a popular video to test
        ])
        
        # Run the command
        print(f"Extracting cookies from {browser} browser with command: {' '.join(extract_cmd)}")
        
        try:
            process = subprocess.run(extract_cmd, capture_output=True, text=True, timeout=30)
            print(f"Command output: {process.stdout}")
            print(f"Command errors: {process.stderr}")
        except subprocess.TimeoutExpired:
            print("Command timed out after 30 seconds")
            return jsonify({
                'message': f"Extraction timed out. The browser profile might be locked or invalid.",
                'status': False,
                'command': ' '.join(extract_cmd)
            }), 400
        
        # Check if cookies file was created successfully
        if not os.path.exists(cookies_file) or os.path.getsize(cookies_file) < 100:
            # Try to create a log of all available browsers for diagnostic purposes
            browser_logs = []
            try:
                if sys.platform == 'win32':
                    # On Windows, list common browser profile locations
                    for browser_name, path in platform_paths['win32'].items():
                        browser_logs.append(f"{browser_name}: {'Exists' if os.path.exists(path) else 'Not found'} - {path}")
                else:
                    # On Linux/Mac, use a command to find browsers
                    find_cmd = ["which", "google-chrome", "firefox", "brave-browser", "chromium-browser"]
                    result = subprocess.run(find_cmd, capture_output=True, text=True)
                    browser_logs.append(f"Found browsers: {result.stdout}")
            except Exception as e:
                browser_logs.append(f"Error checking browsers: {str(e)}")
                
            return jsonify({
                'message': f"Failed to extract cookies from {browser}. Make sure you have logged into YouTube on that browser.",
                'status': False,
                'stdout': process.stdout if 'process' in locals() else "No process output",
                'stderr': process.stderr if 'process' in locals() else "No process error output",
                'browser_logs': browser_logs,
                'platform': sys.platform
            }), 400
            
        return jsonify({
            'message': f"Successfully generated cookies file from {browser}",
            'status': True,
            'file_size': os.path.getsize(cookies_file),
            'platform': sys.platform
        }), 200
        
    except Exception as e:
        return jsonify({
            'message': f"Error generating cookies file: {str(e)}",
            'status': False,
            'traceback': traceback.format_exc(),
            'platform': sys.platform
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

def safe_ffmpeg_process(input_path, output_path, start_time, end_time):
    """Helper function to safely process video clips with ffmpeg"""
    # First try with copy codecs (fastest)
    try:
        cmd = [
            ffmpeg_path if ffmpeg_path else 'ffmpeg',
            '-i', input_path,
            '-ss', str(start_time),
            '-to', str(end_time),
            '-c', 'copy',
            '-y', output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        pass
    
    # If copy fails, try with re-encoding
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
    
def download_via_sieve(video_id, input_path):
    """Download video using Sieve's youtube-downloader function with enhanced error handling and logging"""
    # Check for API key
    if not os.getenv('SIEVE_API_KEY'):
        print("SIEVE_API_KEY not set, skipping Sieve download")
        return False
    
    try:
        # Initialize the youtube-downloader function
        youtube_downloader = sieve.function.get("sieve/youtube-downloader")
        
        # Run the download synchronously
        output = youtube_downloader.run(
            url=f"https://www.youtube.com/watch?v={video_id}",
            download_type="video",
            resolution="144p",  # Lowest quality to reduce bandwidth
            include_audio=True,
            start_time=0,
            end_time=-1,
            include_metadata=False,
            metadata_fields=[],
            include_subtitles=False,
            subtitle_languages=[],
            video_format="mp4",
            audio_format="mp3",
            subtitle_format="vtt"
        )
        
        # Log output type for debugging
        print(f"Output type for video {video_id}: {type(output)}")
        
        # Handle case where output is None
        if output is None:
            raise ValueError(f"Sieve output is None for video {video_id}")
        
        # Convert generator to list if necessary
        if isinstance(output, types.GeneratorType):
            output_list = list(output)
        else:
            output_list = [output]
        
        print(f"Output items count: {len(output_list)}")
        print(f"Output items: {output_list}")
        
        if not output_list:
            raise ValueError(f"Sieve output is empty for video {video_id}")
        
        # Process the output items
        for item in output_list:
            print(f"Item type: {type(item)}")
            if isinstance(item, sieve.File):
                local_path = item.path
                # Ensure the local file exists
                if not os.path.exists(local_path):
                    raise ValueError(f"Local file does not exist: {local_path}")
                # Create the destination directory if it doesn't exist
                os.makedirs(os.path.dirname(input_path), exist_ok=True)
                # Copy the local file to input_path
                shutil.copy(local_path, input_path)
                print(f"Copied local file {local_path} to {input_path}")
                # Verify the copied file
                if not os.path.exists(input_path) or os.path.getsize(input_path) < 1024:
                    raise ValueError(f"Copied file is too small or missing: {input_path}")
                return True
            elif isinstance(item, dict):
                if 'video_url' in item:
                    video_url = item['video_url']
                    # Download from video_url
                    response = requests.get(video_url, stream=True, timeout=90)
                    response.raise_for_status()
                    os.makedirs(os.path.dirname(input_path), exist_ok=True)
                    with open(input_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    print(f"Downloaded video from {video_url} to {input_path}")
                    return True
                elif 'error' in item:
                    raise ValueError(f"Sieve error in item: {item['error']}")
            else:
                raise ValueError(f"Unexpected item type in output: {type(item)}")
        
        raise ValueError("No valid video file or URL found in Sieve output")
    
    except Exception as e:
        print(f"Sieve download failed for video {video_id}: {str(e)}")
        return False
    
    

def download_via_rapidapi(video_id, input_path):
    """Download video using RapidAPI without proxies"""
    try:
        # Step 1: Get download URL from RapidAPI
        api_url = f"https://ytstream-download-youtube-videos.p.rapidapi.com/dl?id={video_id}"
        headers = {
            'x-rapidapi-key': '6820d4d822msh502bdc3b993dbd2p1a24c6jsndfbf9f3bc90b',
            'x-rapidapi-host': 'ytstream-download-youtube-videos.p.rapidapi.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        }
        response = requests.get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        result = response.json()

        # Step 2: Select lowest quality download URL
        download_url = None
        formats = result.get('formats', [])
        if formats:
            sorted_formats = sorted(formats, key=lambda x: int(x.get('height', 0) or 0))
            for fmt in sorted_formats:
                if fmt.get('url'):
                    download_url = fmt['url']
                    print(f"Using RapidAPI format: {fmt.get('qualityLabel', 'unknown')}")
                    break
            if not download_url:
                raise ValueError("No valid download URL found in formats")
        else:
            video_formats = [fmt for fmt in result.get('adaptiveFormats', []) if fmt.get('mimeType', '').startswith('video/')]
            if video_formats:
                sorted_video_formats = sorted(video_formats, key=lambda x: int(x.get('height', 0) or 0))
                download_url = sorted_video_formats[0].get('url')
                print(f"Using RapidAPI adaptive format: {sorted_video_formats[0].get('qualityLabel', 'unknown')}")
            else:
                raise ValueError("No video formats found")

        # Step 3: Download the video
        download_headers = {
            'Referer': 'https://www.youtube.com/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        }
        print(f"Downloading video to path: {input_path}")
        os.makedirs(os.path.dirname(input_path), exist_ok=True)
        with requests.get(download_url, headers=download_headers, stream=True, timeout=90) as r:
            r.raise_for_status()
            with open(input_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        if not os.path.exists(input_path) or os.path.getsize(input_path) < 1024:
            raise ValueError("Downloaded file is too small or empty")
        print(f"RapidAPI downloaded video {video_id} successfully")
        return True
    except Exception as e:
        print(f"RapidAPI download failed: {str(e)}")
        if os.path.exists(input_path):
            try:
                os.remove(input_path)  # Clean up partial downloads
            except:
                pass
        return False

def download_via_ytdlp(video_id, input_path, use_cookies=True):
    """Download video using yt-dlp with a random proxy"""
    try:
        for proxy in PROXY_LIST + [None]:   # Select a random proxy
            proxy_url = None if proxy is None else f"http://{proxy.split(':')[2]}:{proxy.split(':')[3]}@{proxy.split(':')[0]}:{proxy.split(':')[1]}"
            print(f"Using proxy for yt-dlp: {proxy_url}")
            ydl_opts = {
                'format': 'worstvideo[ext=mp4]+worstaudio[ext=m4a]/worst[ext=mp4]',  # Lowest quality
                'outtmpl': input_path,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Linux; Android 10; SM-G960U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Referer': 'https://www.youtube.com/'
                },
                'cookiefile': COOKIES_FILE if use_cookies and os.path.exists(COOKIES_FILE) else None,
                'proxy': proxy_url,  # Add proxy
                'retries': 10,
                'fragment_retries': 10,
            }
            if use_cookies and os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 100:
                print("Using cookies for yt-dlp download")

            os.makedirs(os.path.dirname(input_path), exist_ok=True)
            urls_to_try = [
                f'https://www.youtube.com/watch?v={video_id}',
                f'https://www.youtube.com/embed/{video_id}',
                f'https://youtu.be/{video_id}'
            ]
            last_error = None
            for url in urls_to_try:
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                    break
                except Exception as e:
                    last_error = e
                    print(f"Download attempt failed for {url} with proxy {proxy_url}: {str(e)}")
            else:
                continue  # Try next proxy
            # If we get here, download succeeded
            if not os.path.exists(input_path) or os.path.getsize(input_path) < 1024:
                raise Exception("Downloaded file is too small or empty")
            print(f"yt-dlp downloaded video {video_id} successfully")
            return True
        # If all proxies failed
        raise last_error or Exception("All URL formats failed")
    except Exception as e:
        print(f"yt-dlp download failed: {str(e)}")
        if os.path.exists(input_path):
            try:
                os.remove(input_path)  # Clean up partial downloads
            except:
                pass
        return False


def download_via_pytube(video_id, input_path):
    """Download video using pytube with enhanced options"""
    try:
        yt = YouTube(f'https://www.youtube.com/watch?v={video_id}')
        
        stream = yt.streams.filter(
            file_extension='mp4',
            progressive=True
        ).order_by('resolution').first()  # Changed to select lowest resolution
        
        if not stream:
            raise Exception("No suitable video stream found")
        
        print(f"Downloading video {video_id} with resolution: {stream.resolution}")
        
        os.makedirs(os.path.dirname(input_path), exist_ok=True)
        
        stream.download(
            output_path=os.path.dirname(input_path),
            filename=os.path.basename(input_path))
        
        if not os.path.exists(input_path) or os.path.getsize(input_path) < 1024:
            raise Exception("Downloaded file is too small or empty")
            
        return True
    except Exception as e:
        print(f"pytube download failed: {str(e)}")
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except:
                pass
        return False

def download_video(video_id, input_path):
    """Attempt to download video using multiple methods with prioritization"""
    # First try Sieve
    print("Attempting Sieve")
    if download_via_sieve(video_id, input_path):
        return True
    
    # Then try yt-dlp with cookies
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 100:
        print("Attempting yt-dlp with cookies")
        if download_via_ytdlp(video_id, input_path, use_cookies=True):
            return True
    
    # Then try pytube
    print("Attempting pytube")
    if download_via_pytube(video_id, input_path):
        return True
    
    # Then try RapidAPI
    print("Attempting RapidAPI")
    if download_via_rapidapi(video_id, input_path):
        return True
    
    # Finally try yt-dlp without cookies
    print("Attempting yt-dlp without cookies")
    if download_via_ytdlp(video_id, input_path, use_cookies=False):
        return True
    
    raise Exception("All download methods failed")

@app.route('/merge-clips', methods=['POST'])
def merge_clips_route():
    try:
        if not ffmpeg_available:
            return jsonify({
                'error': 'ffmpeg not available',
                'status': False
            }), 500
            
        data = request.get_json()
        clips = data.get('clips', [])
        cleanup_downloads = data.get('cleanupDownloads', True)
        cleanup_all_downloads = data.get('cleanupAllDownloads', False)
        
        if not clips:
            return jsonify({
                'error': 'No clips provided',
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
                
                if not video_id:
                    raise ValueError(f"Missing videoId in clip: {clip}")
                if end_time <= start_time:
                    raise ValueError(f"Invalid time range: start_time ({start_time}) must be less than end_time ({end_time})")
                
                input_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
                
                # Download video if needed
                if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
                    print(f"Downloading video {video_id}")
                    if not download_video(video_id, input_path):
                        raise Exception(f"Failed to download video {video_id}")
                
                # Verify downloaded file
                if not os.path.exists(input_path) or os.path.getsize(input_path) < 1024:
                    raise ValueError(f"Downloaded file is invalid or too small: {input_path}")
                
                # Create trimmed clip
                clip_output = os.path.join(TMP_DIR, f'clip_{video_id}_{int(start_time)}_{int(end_time)}.mp4')
                
                # Process clip with ffmpeg
                if not safe_ffmpeg_process(input_path, clip_output, start_time, end_time):
                    raise Exception(f"Failed to process clip {video_id}")
                
                if not os.path.exists(clip_output) or os.path.getsize(clip_output) == 0:
                    raise Exception(f"Failed to create clip: {clip_output}")
                
                processed_clips.append({
                    'path': clip_output,
                    'info': clip
                })

            if not processed_clips:
                raise ValueError("No clips were successfully processed")
                
            # Create file list for concatenation
            with open(file_list_path, 'w') as f:
                for clip in processed_clips:
                    f.write(f"file '{clip['path']}'\n")

            time.sleep(1)  # Allow file handles to release

            # Merge clips
            merge_result = subprocess.run([
                ffmpeg_path if ffmpeg_path else 'ffmpeg',
                '-f', 'concat',
                '-safe', '0',
                '-i', file_list_path,
                '-c', 'copy',  # Try stream copy first
                '-y',
                output_path
            ], capture_output=True, text=True)
            
            if merge_result.returncode != 0:
                # If stream copy fails, try re-encoding
                merge_result = subprocess.run([
                    ffmpeg_path if ffmpeg_path else 'ffmpeg',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', file_list_path,
                    '-c:v', 'libx264',
                    '-preset', 'fast',
                    '-c:a', 'aac',
                    '-y',
                    output_path
                ], capture_output=True, text=True)
                
                if merge_result.returncode != 0:
                    raise Exception(f"Failed to merge clips: {merge_result.stderr}")

            # Verify merged file
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise Exception("Merged file is empty or missing")
            
            # Upload to S3
            unique_filename = f"merged_{uuid.uuid4()}.mp4"
            try:
                s3_client.upload_file(
                    output_path,
                    AWS_S3_BUCKET,
                    unique_filename,
                    ExtraArgs={'ContentType': 'video/mp4'}
                )
                
                # Generate presigned URL
                s3_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={
                        'Bucket': AWS_S3_BUCKET,
                        'Key': unique_filename
                    },
                    ExpiresIn=3600
                )
            except Exception as upload_error:
                raise Exception(f"Failed to upload to S3: {str(upload_error)}")

            return jsonify({
                'message': 'Clips merged successfully',
                's3Url': s3_url,
                'clipsInfo': [clip['info'] for clip in processed_clips],
                'success': True,
                'status': True,
                'fileNames3': unique_filename
            })

        except requests.exceptions.HTTPError as http_err:
            status_code = http_err.response.status_code if hasattr(http_err, 'response') else 500
            return jsonify({
                'error': f"HTTP Error {status_code}",
                'status': False,
                'type': 'http_error'
            }), status_code
        except yt_dlp.utils.DownloadError as dl_err:
            return jsonify({
                'error': f"Download failed: {str(dl_err)}",
                'status': False,
                'type': 'download_error'
            }), 400
        except Exception as e:
            return jsonify({
                'error': f"Processing error: {str(e)}",
                'status': False,
                'type': 'processing_error'
            }), 500
        finally:
            # Cleanup temporary files
            for clip in processed_clips:
                try:
                    if os.path.exists(clip['path']):
                        os.remove(clip['path'])
                except Exception:
                    pass
            try:
                if os.path.exists(file_list_path):
                    os.remove(file_list_path)
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass

            # Cleanup downloaded videos if requested
            if cleanup_downloads:
                try:
                    if cleanup_all_downloads:
                        for filename in os.listdir(DOWNLOAD_DIR):
                            if filename.endswith('.mp4'):
                                try:
                                    os.remove(os.path.join(DOWNLOAD_DIR, filename))
                                except Exception:
                                    pass
                    else:
                        video_ids = set(clip.get('videoId') for clip in clips if clip.get('videoId'))
                        for video_id in video_ids:
                            try:
                                os.remove(os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4"))
                            except Exception:
                                pass
                except Exception as cleanup_error:
                    print(f"Warning: Error cleaning up Download folder: {str(cleanup_error)}")

    except Exception as e:
        print(f"Unhandled exception in /merge-clips route:")
        traceback.print_exc()
        return jsonify({
            'error': str(e),
            'status': False,
            'type': 'unexpected_error'
        }), 500
        
if __name__ == '__main__':

    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8000)))