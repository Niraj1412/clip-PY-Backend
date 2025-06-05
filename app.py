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

def download_via_rapidapi(video_id, input_path):
    """Download video using RapidAPI"""
    try:
        api_url = f"https://ytstream-download-youtube-videos.p.rapidapi.com/dl?id={video_id}"
        headers = {
            'x-rapidapi-key': '6820d4d822msh502bdc3b993dbd2p1a24c6jsndfbf9f3bc90b',
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

def download_via_ytdlp(video_id, input_path, use_cookies=True):
    """Download video using yt-dlp with improved error handling"""
    ydl_opts = {
        'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/mp4/best[height<=720]',
        'outtmpl': input_path,
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
                'skip': ['dash', 'hls']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.youtube.com/'
        },
        # Cookie handling
        'cookiefile': COOKIES_FILE if (use_cookies and validate_cookies_file(COOKIES_FILE)) else None,
        # Additional options for better reliability
        'throttled_rate': '100K',
        'sleep_interval': 5,
        'max_sleep_interval': 30,
        'force_ipv4': True,
        'geo_bypass': True,
        'geo_bypass_country': 'US'
    }
    
    try:
        os.makedirs(os.path.dirname(input_path), exist_ok=True)
        
        # Try different URL formats
        url_variants = [
            f'https://www.youtube.com/watch?v={video_id}',
            f'https://www.youtube.com/embed/{video_id}',
            f'https://youtu.be/{video_id}'
        ]
        
        last_error = None
        for url in url_variants:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                
                # Verify the download
                if os.path.exists(input_path) and os.path.getsize(input_path) > 1024:
                    return True
                
            except Exception as e:
                last_error = e
                print(f"Download attempt failed for {url}: {str(e)}")
                continue
        
        if last_error:
            raise last_error
            
        raise Exception("All download attempts failed")
        
    except Exception as e:
        print(f"yt-dlp download failed: {str(e)}")
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except:
                pass
        return False

def download_video(video_id, input_path):
    """Attempt to download video using multiple methods with priority"""
    methods = [
        # Try with cookies first
        lambda: download_via_ytdlp(video_id, input_path, use_cookies=True),
        # Try without cookies
        lambda: download_via_ytdlp(video_id, input_path, use_cookies=False),
        # Try RapidAPI
        lambda: download_via_rapidapi(video_id, input_path),
        # Try pytube fallback
        lambda: download_via_pytube(video_id, input_path)
    ]
    
    last_error = None
    for method in methods:
        try:
            if method():
                return True
        except Exception as e:
            last_error = e
            print(f"Download method failed: {str(e)}")
            continue
    
    error_msg = str(last_error) if last_error else "All download methods failed"
    raise Exception(f"All download methods failed: {error_msg}")

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
    """Ensure we have valid cookies and refresh them if needed"""
    if not validate_cookies_file(COOKIES_FILE):
        print("No valid cookies file found, attempting to generate new cookies")
        try:
            # Try to generate cookies from common browsers
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
                    
                    if validate_cookies_file(COOKIES_FILE):
                        print(f"Successfully generated cookies from {browser}")
                        return True
                    else:
                        print(f"Failed to generate valid cookies from {browser}")
                        print("Command output:", result.stdout)
                        print("Command error:", result.stderr)
                except subprocess.TimeoutExpired:
                    print(f"Timeout generating cookies from {browser}")
                    continue
                except Exception as e:
                    print(f"Error generating cookies from {browser}: {str(e)}")
                    continue
            return False
        except Exception as e:
            print(f"Error generating cookies: {str(e)}")
            return False
    
    # Validate the cookies are still working
    try:
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
        
        if "Sign in to confirm you're not a bot" in result.stderr:
            print("Cookies are no longer valid, attempting to refresh...")
            os.remove(COOKIES_FILE)
            return validate_and_refresh_cookies()
            
        return True
    except Exception as e:
        print(f"Error validating cookies: {str(e)}")
        return False
    
@app.route('/merge-clips', methods=['POST'])
def merge_clips_route():
    
    try:
        # Validate cookies before processing
        if not validate_and_refresh_cookies():
            print("Warning: No valid YouTube cookies available - downloads may fail")

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
                
                # Download video if needed (with retries)
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
                            print(f"Downloading video {video_id} (attempt {attempt + 1})")
                            if not download_video(video_id, input_path):
                                raise Exception(f"Failed to download video {video_id}")
                        
                        # Verify downloaded file
                        if not os.path.exists(input_path) or os.path.getsize(input_path) < 1024:
                            raise ValueError(f"Downloaded file is invalid or too small: {input_path}")
                        break
                    except Exception as e:
                        if attempt == max_retries - 1:
                            raise
                        time.sleep(2)  # Wait before retrying
                        continue
                
                # Create trimmed clip
                clip_output = os.path.join(TMP_DIR, f'clip_{video_id}_{int(start_time)}_{int(end_time)}.mp4')
                
                # Process clip with ffmpeg (with retries)
                for attempt in range(max_retries):
                    try:
                        if not safe_ffmpeg_process(input_path, clip_output, start_time, end_time):
                            raise Exception(f"Failed to process clip {video_id}")
                        
                        if not os.path.exists(clip_output) or os.path.getsize(clip_output) == 0:
                            raise Exception(f"Failed to create clip: {clip_output}")
                        break
                    except Exception as e:
                        if attempt == max_retries - 1:
                            raise
                        time.sleep(1)
                        continue
                
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

            # Merge clips (try both stream copy and re-encode methods)
            merge_success = False
            merge_errors = []
            
            for method in ['copy', 'encode']:
                try:
                    if method == 'copy':
                        cmd = [
                            ffmpeg_path if ffmpeg_path else 'ffmpeg',
                            '-f', 'concat',
                            '-safe', '0',
                            '-i', file_list_path,
                            '-c', 'copy',
                            '-y', output_path
                        ]
                    else:
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
                    
                    merge_success = True
                    break
                
                except Exception as e:
                    merge_errors.append(f"{method} method failed: {str(e)}")
                    continue
            
            if not merge_success:
                raise Exception(f"All merge methods failed:\n" + "\n".join(merge_errors))
            
            # Upload to S3 with retries
            unique_filename = f"merged_{uuid.uuid4()}.mp4"
            max_upload_attempts = 3
            s3_url = None
            
            for attempt in range(max_upload_attempts):
                try:
                    s3_client.upload_file(
                        output_path,
                        AWS_S3_BUCKET,
                        unique_filename,
                        ExtraArgs={
                            'ContentType': 'video/mp4',
                            'ACL': 'public-read'  # Ensure the file is accessible
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
                    break
                except Exception as upload_error:
                    if attempt == max_upload_attempts - 1:
                        raise Exception(f"Failed to upload to S3 after {max_upload_attempts} attempts: {str(upload_error)}")
                    time.sleep(2)
                    continue

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
            # Enhanced cleanup with error handling
            cleanup_errors = []
            
            def safe_remove(path):
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    cleanup_errors.append(f"Failed to remove {path}: {str(e)}")
            
            # Cleanup processed clips
            for clip in processed_clips:
                safe_remove(clip.get('path'))
            
            # Cleanup other temp files
            safe_remove(file_list_path)
            safe_remove(output_path)
            
            if cleanup_errors:
                print("Cleanup warnings:", "\n".join(cleanup_errors))

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