from flask import Flask, request, jsonify
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
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
from pytube import YouTube
import random
import logging
from selenium import webdriver
from selenium.webdriver.common.by import By
import http.cookiejar
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException






load_dotenv()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('app')
logger.setLevel(logging.INFO)

# Initialize Flask app
app = Flask(__name__)
CORS(app)

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

def refresh_cookies():
    try:
        email = os.getenv('YOUTUBE_EMAIL')
        password = os.getenv('YOUTUBE_PASSWORD')
        if not email or not password:
            logger.error("YouTube email or password not set in .env file")
            raise ValueError("YouTube email and password must be set in .env file")

        options = webdriver.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.binary_location = '/usr/bin/chromium-browser'

        driver = webdriver.Chrome(
            service=Service('/usr/bin/chromedriver'),
            options=options
        )

        try:
            logger.info("Navigating to YouTube login page")
            driver.get("https://accounts.google.com/ServiceLogin?service=youtube")

            # Enter email
            logger.info("Entering email")
            email_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "identifierId"))
            )
            email_field.send_keys(email)
            driver.find_element(By.ID, "identifierNext").click()

            # Handle potential consent screen
            try:
                consent_button = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'I agree')]"))
                )
                consent_button.click()
                logger.info("Clicked consent button")
            except TimeoutException:
                logger.info("No consent button found")

            # Wait for password field with extended timeout
            logger.info("Waiting for password field")
            password_field = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.NAME, "password"))
            )

            # Enter password
            logger.info("Entering password")
            password_field.send_keys(password)
            driver.find_element(By.ID, "passwordNext").click()

            # Handle potential CAPTCHA or verification
            try:
                # Check for CAPTCHA (simplified detection)
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//iframe[@title='reCAPTCHA']"))
                )
                logger.warning("CAPTCHA detected; manual intervention or CAPTCHA solver required")
                # Placeholder for CAPTCHA solving service integration
                # Example: Integrate with 2Captcha or Anti-Captcha
                return False  # For now, fail gracefully if CAPTCHA is detected
            except TimeoutException:
                logger.info("No CAPTCHA detected")

            # Wait for login to complete
            logger.info("Waiting for login to complete")
            WebDriverWait(driver, 30).until(
                EC.url_contains("youtube.com"),
                message="Login failed or took too long; possible CAPTCHA or consent screen"
            )
            logger.info("Login successful")

            # Save cookies
            cookies = driver.get_cookies()
            cookie_jar = http.cookiejar.MozillaCookieJar(COOKIES_FILE)
            for cookie in cookies:
                cookie_jar.set_cookie(http.cookiejar.Cookie(
                    version=0,
                    name=cookie['name'],
                    value=cookie['value'],
                    port=None,
                    port_specified=False,
                    domain=cookie['domain'],
                    domain_specified=True,
                    domain_initial_dot=cookie['domain'].startswith('.'),
                    path=cookie['path'],
                    path_specified=True,
                    secure=cookie['secure'],
                    expires=cookie.get('expiry'),
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False
                ))
            cookie_jar.save(ignore_discard=True, ignore_expires=True)

            if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
                logger.info("Cookies saved successfully to %s", COOKIES_FILE)
                return True
            else:
                logger.error("Cookies file is empty or not created")
                return False

        except TimeoutException as te:
            logger.error(f"Timeout during login: {str(te)} - Possible CAPTCHA or consent screen")
            return False
        except Exception as e:
            logger.error(f"Error during login process: {str(e)}")
            return False
        finally:
            driver.quit()

    except Exception as e:
        logger.error(f"Failed to refresh cookies: {str(e)}")
        return False






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
    
    
def validate_cookies():
    """Validate cookies by testing them with yt-dlp."""
    if not os.path.exists(COOKIES_FILE) or os.path.getsize(COOKIES_FILE) < 100:
        return False
    try:
        test_cmd = [
            sys.executable, "-m", "yt_dlp",
            "--cookies", COOKIES_FILE,
            "--skip-download",
            "--print", "title",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # Test video
        ]
        process = subprocess.run(test_cmd, capture_output=True, text=True, timeout=15)
        if process.returncode == 0 and "Sign in to confirm" not in process.stderr:
            print("Cookies validated successfully.")
            return True
        print("Cookies invalid based on test.")
        return False
    except Exception as e:
        print(f"Error validating cookies: {str(e)}")
        return False

PROXY_LIST = [
    "38.154.227.167:5868:pzvokxqt:v17333r03zxw",
    "198.23.239.134:6540:pzvokxqt:v17333r03zxw",
    "207.244.217.165:6712:pzvokxqt:v17333r03zxw",
    "107.172.163.27:6543:pzvokxqt:v17333r03zxw",
    "216.10.27.159:6837:pzvokxqt:v17333r03zxw",
    "136.0.207.84:6661:pzvokxqt:v17333r03zxw",
    "64.64.118.149:6732:pzvokxqt:v17333r03zxw",
    "142.147.128.93:6593:pzvokxqt:v17333r03zxw",
    "104.239.105.125:6655:pzvokxqt:v17333r03zxw",
    "206.41.172.74:6634:pzvokxqt:v17333r03zxw"
]

def get_random_proxy():
    """Return a random proxy from PROXY_LIST in the correct format."""
    proxy = random.choice(PROXY_LIST)
    host, port, user, pwd = proxy.split(":")
    return f"http://{user}:{pwd}@{host}:{port}"

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
    
def auto_generate_cookies():
    """Generate cookies using Selenium with a fallback to backup cookies."""
    if validate_cookies():
        print("Existing cookies are valid.")
        return True

    if refresh_cookies() and validate_cookies():
        return True

    # Fallback to backup cookies
    backup_cookies_path = os.path.join(BASE_DIR, 'backup_youtube_cookies.txt')
    if os.path.exists(backup_cookies_path) and validate_cookies_file(backup_cookies_path):
        shutil.copy(backup_cookies_path, COOKIES_FILE)
        if validate_cookies():
            print("Using backup cookies.")
            return True

    print("Failed to generate or use valid cookies.")
    return False

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
    try:
        if 'cookiesFile' not in request.files:
            return jsonify({'message': "No cookies file provided", 'status': False}), 400

        file = request.files['cookiesFile']
        if file.filename == '':
            return jsonify({'message': "No cookies file selected", 'status': False}), 400

        cookies_file = COOKIES_FILE
        file.save(cookies_file)

        if not validate_cookies_file(cookies_file):
            os.remove(cookies_file)
            return jsonify({'message': "Invalid cookies file format or size", 'status': False}), 400

        return jsonify({'message': "Cookies file uploaded successfully", 'status': True}), 200
    except Exception as e:
        return jsonify({'message': f"Error uploading cookies file: {str(e)}", 'status': False}), 500

@app.route('/generate-cookies', methods=['POST'])
def generate_cookies():
    """
    Automatically generate YouTube cookies using Selenium by logging in with provided credentials.
    Requires YOUTUBE_EMAIL and YOUTUBE_PASSWORD in .env file.
    """
    try:
        # Check for required credentials
        email = os.getenv('YOUTUBE_EMAIL')
        password = os.getenv('YOUTUBE_PASSWORD')
        if not email or not password:
            return jsonify({
                'message': "YouTube email and password must be set in .env file",
                'status': False
            }), 400

        # Set up Selenium with Chrome (headless mode)
        options = webdriver.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        driver = webdriver.Chrome(options=options)
        
        try:
            # Navigate to YouTube login page
            driver.get("https://accounts.google.com/ServiceLogin?service=youtube")
            
            # Enter email
            email_field = driver.find_element(By.ID, "identifierId")
            email_field.send_keys(email)
            driver.find_element(By.ID, "identifierNext").click()
            time.sleep(2)  # Wait for page transition

            # Enter password
            password_field = driver.find_element(By.NAME, "password")
            password_field.send_keys(password)
            driver.find_element(By.ID, "passwordNext").click()
            time.sleep(5)  # Wait for login to complete and cookies to be set

            # Extract cookies
            cookies = driver.get_cookies()

            # Save cookies in Netscape format
            cookie_jar = http.cookiejar.MozillaCookieJar(COOKIES_FILE)
            for cookie in cookies:
                cookie_jar.set_cookie(http.cookiejar.Cookie(
                    version=0,
                    name=cookie['name'],
                    value=cookie['value'],
                    port=None,
                    port_specified=False,
                    domain=cookie['domain'],
                    domain_specified=True,
                    domain_initial_dot=cookie['domain'].startswith('.'),
                    path=cookie['path'],
                    path_specified=True,
                    secure=cookie['secure'],
                    expires=cookie.get('expiry'),
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False
                ))
            cookie_jar.save(ignore_discard=True, ignore_expires=True)

            return jsonify({
                'message': "Cookies generated and saved successfully",
                'status': True,
                'cookie_count': len(cookies)
            }), 200
        finally:
            driver.quit()

    except Exception as e:
        return jsonify({
            'message': f"Error generating cookies: {str(e)}",
            'status': False,
            'traceback': traceback.format_exc()
        }), 500

@app.route('/check-cookies', methods=['GET'])
def check_cookies():
    try:
        if not os.path.exists(COOKIES_FILE) or os.path.getsize(COOKIES_FILE) < 100:
            return jsonify({
                'message': "No valid cookies file found",
                'status': False,
                'has_cookies': False
            }), 200

        if not validate_cookies_file(COOKIES_FILE):
            return jsonify({
                'message': "Cookies file exists but has invalid format",
                'status': True,
                'has_cookies': True,
                'valid_format': False
            }), 200

        # Test cookies with yt-dlp
        test_cmd = [
            sys.executable, "-m", "yt_dlp",
            "--cookies", COOKIES_FILE,
            "--skip-download",
            "--print", "title",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ]
        process = subprocess.run(test_cmd, capture_output=True, text=True, timeout=15)

        if process.returncode != 0 or "Sign in to confirm" in process.stderr:
            return jsonify({
                'message': "Cookies exist but failed authentication test",
                'status': True,
                'has_cookies': True,
                'valid_format': True,
                'works_with_youtube': False,
                'error': process.stderr
            }), 200

        return jsonify({
            'message': "Valid cookies file found and working",
            'status': True,
            'has_cookies': True,
            'valid_format': True,
            'works_with_youtube': True,
            'file_size': os.path.getsize(COOKIES_FILE),
            'last_modified': time.ctime(os.path.getmtime(COOKIES_FILE))
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

def download_via_rapidapi(video_id, input_path, use_proxy=True):
    """Download video using RapidAPI with proxy support."""
    try:
        api_url = f"https://ytstream-download-youtube-videos.p.rapidapi.com/dl?id={video_id}"
        headers = {
            'x-rapidapi-key': '6820d4d822msh502bdc3b993dbd2p1a24c6jsndfbf9f3bc90b',  # Replace with your key
            'x-rapidapi-host': 'ytstream-download-youtube-videos.p.rapidapi.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        }
        proxies = {"http": get_random_proxy(), "https": get_random_proxy()} if use_proxy else None
        response = requests.get(api_url, headers=headers, proxies=proxies, timeout=30)
        response.raise_for_status()
        result = response.json()
        download_url = next((fmt['url'] for fmt in result.get('adaptiveFormats', []) if fmt.get('url')), None)
        if not download_url:
            raise ValueError("No download URL found")
        with requests.get(download_url, stream=True, proxies=proxies, timeout=90) as r:
            r.raise_for_status()
            os.makedirs(os.path.dirname(input_path), exist_ok=True)
            with open(input_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        if os.path.exists(input_path) and os.path.getsize(input_path) > 1024:
            print(f"RapidAPI download succeeded for {video_id}")
            return True
    except Exception as e:
        print(f"RapidAPI download failed: {str(e)}")
    return False


def download_via_ytdlp(video_id, input_path, use_cookies=True, use_proxy=True):
    """Download video using yt-dlp with retries."""
    ydl_opts = {
        'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/mp4/best[height<=720]',
        'outtmpl': input_path,
        'retries': 5,
        'proxy': get_random_proxy() if use_proxy else None,
        'cookiefile': COOKIES_FILE if use_cookies and os.path.exists(COOKIES_FILE) else None,
        'quiet': False,
        'no_warnings': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Referer': 'https://www.youtube.com/'
        }
    }
    try:
        os.makedirs(os.path.dirname(input_path), exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
        if os.path.exists(input_path) and os.path.getsize(input_path) > 1024:
            print(f"yt-dlp download succeeded for {video_id}")
            return True
    except Exception as e:
        print(f"yt-dlp download failed: {str(e)}")
    return False


def download_via_pytube(video_id, input_path):
    """Download video using pytube with retries."""
    for attempt in range(3):
        try:
            yt = YouTube(f'https://www.youtube.com/watch?v={video_id}')
            stream = yt.streams.filter(file_extension='mp4', progressive=True).order_by('resolution').desc().first()
            if stream:
                stream.download(output_path=os.path.dirname(input_path), filename=os.path.basename(input_path))
                if os.path.exists(input_path) and os.path.getsize(input_path) > 1024:
                    print(f"pytube download succeeded for {video_id}")
                    return True
        except Exception as e:
            print(f"pytube attempt {attempt+1} failed: {str(e)}")
            time.sleep(5)  # Delay between retries
    return False

def download_video(video_id, input_path, use_proxy=True):
    """Download video using multiple methods with robust cookie handling."""
    if not validate_cookies():
        logger.info("Cookies invalid or missing, refreshing...")
        if not refresh_cookies() or not validate_cookies():
            logger.warning("Could not generate valid cookies, proceeding without them")

    methods = [
        lambda: download_via_ytdlp(video_id, input_path, use_cookies=True, use_proxy=use_proxy),
        lambda: download_via_pytube(video_id, input_path),
        lambda: download_via_rapidapi(video_id, input_path, use_proxy=use_proxy),
        lambda: download_via_ytdlp(video_id, input_path, use_cookies=False, use_proxy=use_proxy)
    ]

    for method in methods:
        try:
            if method():
                logger.info(f"Successfully downloaded video {video_id}")
                return True
        except Exception as e:
            logger.error(f"Download method failed for {video_id}: {str(e)}")
    
    raise Exception("All download methods failed")

@app.route('/merge-clips', methods=['POST'])
def merge_clips_route():
    try:
        if not ffmpeg_available:
            return jsonify({'error': 'ffmpeg not available', 'status': False}), 500

        # Generate fresh cookies before processing clips
        print("Generating fresh cookies before merging clips")
        if not auto_generate_cookies():
            print("Warning: Failed to generate fresh cookies, proceeding with existing ones")

        data = request.get_json()
        clips = data.get('clips', [])
        cleanup_downloads = data.get('cleanupDownloads', True)
        cleanup_all_downloads = data.get('cleanupAllDownloads', False)
        use_proxy = data.get('useProxy', True)  # New option to toggle proxy usage

        if not clips:
            return jsonify({'error': 'No clips provided', 'status': False}), 400

        timestamp = int(time.time())
        file_list_path = os.path.join(TMP_DIR, f'filelist_{timestamp}.txt')
        output_path = os.path.join(TMP_DIR, f'merged_clips_{timestamp}.mp4')
        processed_clips = []

        try:
            for clip in clips:
                video_id = clip.get('videoId')
                start_time = float(clip.get('startTime', 0))
                end_time = float(clip.get('endTime', 0))

                if not video_id:
                    raise ValueError(f"Missing videoId in clip: {clip}")
                if end_time <= start_time:
                    raise ValueError(f"Invalid time range: start_time ({start_time}) >= end_time ({end_time})")

                input_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")

                if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
                    print(f"Downloading video {video_id}")
                    if not download_video(video_id, input_path, use_proxy=use_proxy):
                        raise Exception(f"Failed to download video {video_id}")

                if not os.path.exists(input_path) or os.path.getsize(input_path) < 1024:
                    raise ValueError(f"Downloaded file is invalid or too small: {input_path}")

                clip_output = os.path.join(TMP_DIR, f'clip_{video_id}_{int(start_time)}_{int(end_time)}.mp4')
                if not safe_ffmpeg_process(input_path, clip_output, start_time, end_time):
                    raise Exception(f"Failed to process clip {video_id}")

                if not os.path.exists(clip_output) or os.path.getsize(clip_output) == 0:
                    raise Exception(f"Failed to create clip: {clip_output}")

                processed_clips.append({'path': clip_output, 'info': clip})

            if not processed_clips:
                raise ValueError("No clips were successfully processed")

            with open(file_list_path, 'w') as f:
                for clip in processed_clips:
                    f.write(f"file '{clip['path']}'\n")

            time.sleep(1)

            merge_result = subprocess.run([
                ffmpeg_path if ffmpeg_path else 'ffmpeg',
                '-f', 'concat',
                '-safe', '0',
                '-i', file_list_path,
                '-c', 'copy',
                '-y',
                output_path
            ], capture_output=True, text=True)

            if merge_result.returncode != 0:
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

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise Exception("Merged file is empty or missing")

            unique_filename = f"merged_{uuid.uuid4()}.mp4"
            try:
                s3_client.upload_file(
                    output_path,
                    AWS_S3_BUCKET,
                    unique_filename,
                    ExtraArgs={'ContentType': 'video/mp4'}
                )
                s3_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': AWS_S3_BUCKET, 'Key': unique_filename},
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

        finally:
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