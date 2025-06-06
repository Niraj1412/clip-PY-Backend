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
from botocore.exceptions import NoCredentialsError, ClientError
import uuid
import yt_dlp
import traceback
import re
import hashlib

load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# AWS S3 Configuration
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_S3_BUCKET = os.getenv('AWS_S3_BUCKET', 'clipsmart')

# Initialize S3 client with configuration
s3_client = boto3.client(
    's3',
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    config=boto3.session.Config(
        signature_version='s3v4',
        retries={
            'max_attempts': 5,
            'mode': 'standard'
        }
    )
)

def sanitize_filename(filename):
    """Sanitize filename by removing special characters and replacing spaces with underscores"""
    # Remove special characters
    filename = re.sub(r'[^\w\s-]', '', filename)
    # Replace spaces with underscores
    filename = re.sub(r'\s+', '_', filename)
    # Remove any remaining special characters that might cause issues
    filename = re.sub(r'[^\w.-]', '', filename)
    return filename[:100]  # Limit filename length

def calculate_md5(file_path):
    """Calculate MD5 hash of a file"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

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

try:
    test_file_path = os.path.join(DOWNLOAD_DIR, 'test_write.txt')
    with open(test_file_path, 'w') as f:
        f.write('Test write access')
    os.remove(test_file_path)
    print(f"Write test successful for {DOWNLOAD_DIR}")
except Exception as e:
    print(f"WARNING: Cannot write to {DOWNLOAD_DIR}: {str(e)}")

try:
    test_file_path = os.path.join(TMP_DIR, 'test_write.txt')
    with open(test_file_path, 'w') as f:
        f.write('Test write access')
    os.remove(test_file_path)
    print(f"Write test successful for {TMP_DIR}")
except Exception as e:
    print(f"WARNING: Cannot write to {TMP_DIR}: {str(e)}")

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

def validate_video_file(file_path):
    """Validate that a video file exists and is readable by ffmpeg"""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File does not exist: {file_path}")
    
    if os.path.getsize(file_path) < 1024:
        raise ValueError(f"File is too small (likely corrupted): {file_path}")
    
    try:
        probe_cmd = [
            ffmpeg_path if ffmpeg_path else 'ffmpeg',
            '-v', 'error',
            '-i', file_path,
            '-f', 'null',
            '-t', '1',
            '-'
        ]
        print(f"Validating video file: {' '.join(probe_cmd)}")
        result = subprocess.run(probe_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise ValueError(f"Invalid video file: {result.stderr}")
    except Exception as e:
        raise ValueError(f"Video validation failed: {str(e)}")

def upload_to_s3(file_path, bucket, object_name=None):
    if object_name is None:
        object_name = os.path.basename(file_path)
    
    try:
        # Validate file before uploading
        validate_video_file(file_path)
        
        # Calculate file MD5 for verification
        file_md5 = calculate_md5(file_path)
        
        # Upload with extra verification
        extra_args = {
            'Metadata': {'md5checksum': file_md5},
            'ContentType': 'video/mp4'
        }
        
        # Use multipart upload for larger files
        config = boto3.s3.transfer.TransferConfig(
            multipart_threshold=1024 * 1024 * 25,  # 25MB
            max_concurrency=10,
            multipart_chunksize=1024 * 1024 * 25,  # 25MB
            use_threads=True
        )
        
        s3_client.upload_file(
            file_path,
            bucket,
            object_name,
            ExtraArgs=extra_args,
            Config=config,
            Callback=None
        )
        
        # Verify the upload by checking metadata
        try:
            head = s3_client.head_object(Bucket=bucket, Key=object_name)
            if 'md5checksum' in head.get('Metadata', {}):
                if head['Metadata']['md5checksum'] != file_md5:
                    print("Warning: MD5 checksum mismatch after upload")
                    # Attempt to delete the potentially corrupted upload
                    try:
                        s3_client.delete_object(Bucket=bucket, Key=object_name)
                    except Exception as delete_error:
                        print(f"Failed to delete corrupted upload: {str(delete_error)}")
                    return False, None
        except ClientError as verify_error:
            print(f"Failed to verify upload: {str(verify_error)}")
            return False, None
        
        presigned_url = s3_client.generate_presigned_url('get_object',
                                                      Params={'Bucket': bucket,
                                                              'Key': object_name},
                                                      ExpiresIn=604800)
        return True, presigned_url
    except FileNotFoundError:
        print(f"The file {file_path} was not found")
        return False, None
    except NoCredentialsError:
        print("Credentials not available")
        return False, None
    except ClientError as e:
        if e.response['Error']['Code'] == 'BadDigest':
            print(f"Content-MD5 mismatch error: {str(e)}")
            # Attempt to delete the potentially corrupted upload
            try:
                s3_client.delete_object(Bucket=bucket, Key=object_name)
            except Exception as delete_error:
                print(f"Failed to delete corrupted upload: {str(delete_error)}")
        else:
            print(f"AWS ClientError uploading to S3: {str(e)}")
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

@app.route('/merge-clips', methods=['POST'])
def merge_clips_route():
    try:
        if not ffmpeg_available:
            return jsonify({
                'error': 'ffmpeg not available. Please install ffmpeg and ensure it is in your system PATH.',
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
        intermediate_files = []
        unique_filename = None
        s3_url = None
        
        try:
            for clip in clips:
                video_id = clip.get('videoId')
                transcript_text = clip.get('transcriptText', '')
                start_time = float(clip.get('startTime', 0))
                end_time = float(clip.get('endTime', 0))
                
                if not video_id:
                    raise ValueError(f"Missing videoId in clip: {clip}")
                
                if end_time <= start_time:
                    raise ValueError(f"Invalid time range: start_time ({start_time}) must be less than end_time ({end_time})")
                
                input_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
                
                # Auto-download video if not found
                if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
                    print(f"Video {video_id} not found or empty. Attempting download...")
                    
                    download_success = False
                    try:
                        # Try yt-dlp first as it's more reliable
                        print(f"Attempting download via yt-dlp for video {video_id}")
                        
                        ydl_opts = {
                            'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/mp4/best[height<=720]',
                            'outtmpl': input_path,
                            'quiet': False,
                            'verbose': True,
                            'noplaylist': True,
                            'progress_hooks': [lambda d: print(f"yt-dlp: {d['status']}") if d['status'] in ['downloading', 'finished'] else None],
                            'nocheckcertificate': True,
                            'ignoreerrors': True,
                            'no_warnings': False,
                            'retries': 10,
                            'fragment_retries': 10,
                            'skip_unavailable_fragments': True,
                            'extractor_retries': 5,
                            'file_access_retries': 5,
                            'hls_prefer_native': True,
                            'hls_use_mpegts': True,
                            'external_downloader_args': ['ffmpeg:-nostats', 'ffmpeg:-loglevel', 'ffmpeg:warning'],
                            'http_headers': {
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                                'Accept-Language': 'en-US,en;q=0.5',
                                'Sec-Fetch-Mode': 'navigate',
                                'Dnt': '1',
                                'Connection': 'keep-alive',
                                'Upgrade-Insecure-Requests': '1',
                                'Sec-Fetch-Dest': 'document',
                                'Sec-Fetch-Site': 'none',
                                'Sec-Fetch-User': '?1',
                                'Sec-Ch-Ua': '" Not A;Brand";v="99", "Chromium";v="91"',
                                'Sec-Ch-Ua-Mobile': '?0'
                            }
                        }
                        
                        try:
                            print(f"Starting yt-dlp download for video {video_id}")
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                urls_to_try = [
                                    f'https://www.youtube.com/watch?v={video_id}',
                                    f'https://www.youtube.com/embed/{video_id}',
                                    f'https://youtu.be/{video_id}'
                                ]

                                last_error = None
                                for url in urls_to_try:
                                    try:
                                        ydl.download([url])
                                        if os.path.exists(input_path) and os.path.getsize(input_path) > 1024:
                                            download_success = True
                                            break
                                    except Exception as e:
                                        last_error = e
                                        print(f"Download attempt failed with URL {url}: {str(e)}")
                                        continue

                                if not download_success:
                                    raise last_error if last_error else Exception("All download attempts failed")

                        except Exception as download_error:
                            for f in [input_path, f"{input_path}.part"]:
                                try:
                                    if os.path.exists(f):
                                        os.remove(f)
                                except:
                                    pass
                            raise download_error
                            
                    except Exception as ytdlp_error:
                        print(f"yt-dlp download failed: {str(ytdlp_error)}")
                        traceback.print_exc()
                        
                        try:
                            print(f"Attempting download via RapidAPI for video {video_id}")
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
                            total_size = len(video_content)
                            print(f"RapidAPI Download completed (in memory). Total size: {total_size} bytes")

                            if total_size < 1024:
                                raise ValueError(f"Downloaded file via RapidAPI is too small or empty.")

                            with open(input_path, 'wb') as f:
                                f.write(video_content)
                            print(f"Successfully wrote video content to {input_path}")
                            download_success = True
                                    
                        except (requests.exceptions.RequestException, ValueError, KeyError) as rapid_api_error:
                            print(f"RapidAPI download failed: {str(rapid_api_error)}")
                            raise Exception(f"All download methods failed for video {video_id}. yt-dlp error: {ytdlp_error}, RapidAPI error: {rapid_api_error}")
                    
                    if not download_success:
                        raise Exception(f"Download failed for video {video_id} without specific error.")
                        
                    if not os.path.exists(input_path):
                        raise ValueError(f"File does not exist after download attempt: {input_path}")
                        
                    if os.path.getsize(input_path) < 1024:
                        raise ValueError(f"Downloaded file is too small: {os.path.getsize(input_path)} bytes")
                        
                    try:
                        validate_video_file(input_path)
                        print(f"Successfully downloaded and validated video {video_id}")
                    except Exception as validate_err:
                        try:
                            os.remove(input_path)
                        except OSError as rm_err:
                            print(f"Warning: Failed to remove invalid file {input_path}: {rm_err}")
                        raise ValueError(f"File validation check failed: {validate_err}")

                # Create trimmed clip with a safe filename
                safe_transcript = sanitize_filename(transcript_text[:30]) if transcript_text else ""
                clip_filename = f'clip_{video_id}_{int(start_time)}_{int(end_time)}'
                if safe_transcript:
                    clip_filename += f'_{safe_transcript}'
                clip_output = os.path.join(TMP_DIR, f'{clip_filename}.mp4')
                
                try:
                    validate_video_file(input_path)
                    
                    # First check if the video is VP9 format and needs conversion
                    probe_cmd = [
                        ffmpeg_path if ffmpeg_path else 'ffmpeg',
                        '-i', input_path
                    ]
                    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
                    
                    needs_conversion = 'vp09' in probe_result.stderr.lower()
                    intermediate_path = None
                    
                    if needs_conversion:
                        print(f"Video {video_id} is in VP9 format, converting to H.264 first...")
                        intermediate_path = os.path.join(TMP_DIR, f'intermediate_{video_id}.mp4')
                        
                        convert_cmd = [
                            ffmpeg_path if ffmpeg_path else 'ffmpeg',
                            '-i', input_path,
                            '-c:v', 'libx264',
                            '-c:a', 'aac',
                            '-pix_fmt', 'yuv420p',
                            '-preset', 'medium',
                            '-y',
                            intermediate_path
                        ]
                        print(f"Running conversion command: {' '.join(convert_cmd)}")
                        convert_result = subprocess.run(convert_cmd, capture_output=True, text=True)
                        
                        if convert_result.returncode != 0:
                            print(f"Conversion failed: {convert_result.stderr}")
                            raise Exception(f"Failed to convert VP9 video: {convert_result.stderr}")
                        
                        input_path = intermediate_path
                        intermediate_files.append(intermediate_path)
                    
                    # Use direct ffmpeg command for trimming
                    cmd = [
                        ffmpeg_path if ffmpeg_path else 'ffmpeg',
                        '-err_detect', 'aggressive',
                        '-i', input_path,
                        '-ss', str(start_time),
                        '-to', str(end_time),
                        '-c:v', 'libx264',
                        '-c:a', 'aac',
                        '-pix_fmt', 'yuv420p',
                        '-preset', 'medium',
                        '-movflags', '+faststart',
                        '-y',
                        clip_output
                    ]
                    
                    print(f"Running ffmpeg command: {' '.join(cmd)}")
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    
                    if result.returncode != 0:
                        print(f"ffmpeg error: {result.stderr}")
                        raise Exception(f"ffmpeg command failed: {result.stderr}")
                    
                    if not os.path.exists(clip_output) or os.path.getsize(clip_output) == 0:
                        raise Exception(f"Failed to create clip: {clip_output}")
                        
                    # Validate the created clip
                    try:
                        validate_video_file(clip_output)
                    except Exception as validate_err:
                        raise Exception(f"Created clip is invalid: {str(validate_err)}")
                        
                    processed_clips.append({
                        'path': clip_output,
                        'info': clip
                    })
                    
                except Exception as clip_error:
                    raise Exception(f"Error processing clip {video_id}: {str(clip_error)}")

            if not processed_clips:
                raise ValueError("No clips were successfully processed")
                
            # Create file list for concatenation
            with open(file_list_path, 'w') as f:
                for clip in processed_clips:
                    f.write(f"file '{clip['path']}'\n")

            time.sleep(1)  # Small delay to ensure file system is ready

            # Merge all clips using direct ffmpeg command
            try:
                cmd = [
                    ffmpeg_path if ffmpeg_path else 'ffmpeg',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', file_list_path,
                    '-c:v', 'libx264',
                    '-c:a', 'aac',
                    '-movflags', '+faststart',
                    '-y',
                    output_path
                ]
                
                print(f"Running ffmpeg merge command: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True)
                
                if result.returncode != 0:
                    print(f"ffmpeg merge error: {result.stderr}")
                    raise Exception(f"ffmpeg merge error: {result.stderr}")
                
                if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                    raise Exception(f"Failed to create merged file: {output_path}")
                    
                # Validate the merged output
                validate_video_file(output_path)
                
            except subprocess.CalledProcessError as e:
                print(f"ffmpeg merge command failed: {e.stderr.decode() if e.stderr else str(e)}")
                raise Exception(f"ffmpeg merge command failed: {e.stderr.decode() if e.stderr else str(e)}")
            except Exception as merge_error:
                raise Exception(f"Error merging clips: {str(merge_error)}")

            # Upload the merged video to S3
            unique_filename = f"merged_{uuid.uuid4()}_{timestamp}.mp4"
            upload_success = False
            retries = 3
            last_upload_error = None
            
            for attempt in range(retries):
                try:
                    success, s3_url = upload_to_s3(output_path, AWS_S3_BUCKET, object_name=unique_filename)
                    if success:
                        upload_success = True
                        break
                    else:
                        last_upload_error = "Upload failed without specific error"
                except Exception as upload_error:
                    last_upload_error = str(upload_error)
                    print(f"Upload attempt {attempt + 1} failed: {last_upload_error}")
                    time.sleep(2 ** attempt)  # Exponential backoff
            
            if not upload_success:
                raise Exception(f"Failed to upload merged video to S3 after {retries} attempts. Last error: {last_upload_error}")

        except Exception as e:
            # Clean up any temporary files
            for clip in processed_clips:
                try:
                    if os.path.exists(clip['path']):
                        os.remove(clip['path'])
                except Exception:
                    pass
                    
            for intermediate_file in intermediate_files:
                try:
                    if os.path.exists(intermediate_file):
                        os.remove(intermediate_file)
                except Exception:
                    pass
                    
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass
                    
            print(f"Error processing merge-clips request: {str(e)}")
            traceback.print_exc()
            raise e
        finally:
            try:
                if os.path.exists(file_list_path):
                    os.remove(file_list_path)
            except Exception:
                pass

        # Clean up individual clips after successful merge and upload
        for clip in processed_clips:
            try:
                if os.path.exists(clip['path']):
                    os.remove(clip['path'])
            except Exception:
                pass

        # Clean up intermediate files
        for intermediate_file in intermediate_files:
            try:
                if os.path.exists(intermediate_file):
                    os.remove(intermediate_file)
            except Exception:
                pass

        # Clean up the merged file from tmp after successful upload
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass

        # Clean up the original video files from Download folder if cleanup is requested
        if cleanup_downloads:
            try:
                if cleanup_all_downloads:
                    removed_count = 0
                    for filename in os.listdir(DOWNLOAD_DIR):
                        if filename.endswith('.mp4'):
                            file_path = os.path.join(DOWNLOAD_DIR, filename)
                            try:
                                os.remove(file_path)
                                removed_count += 1
                                print(f"Removed video file: {file_path}")
                            except Exception as e:
                                print(f"Failed to remove {file_path}: {str(e)}")
                    
                    print(f"Aggressive cleanup: removed {removed_count} video files from Download folder")
                else:
                    video_ids = set(clip.get('videoId') for clip in clips if clip.get('videoId'))
                    cleaned_videos = []
                    
                    for video_id in video_ids:
                        video_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
                        if os.path.exists(video_path):
                            os.remove(video_path)
                            cleaned_videos.append(video_id)
                            print(f"Removed original video file: {video_path}")
                    
                    print(f"Cleaned up {len(cleaned_videos)} video files from Download folder")
                
                remaining_files = os.listdir(DOWNLOAD_DIR)
                if not remaining_files:
                    print(f"Download folder is empty, maintaining directory structure")
            except Exception as cleanup_error:
                print(f"Warning: Error cleaning up Download folder: {str(cleanup_error)}")

        return jsonify({
            'message': 'Clips merged successfully',
            'outputPath': output_path,
            's3Url': s3_url,
            'clipsInfo': [clip['info'] for clip in processed_clips],
            'success': True,
            'status': True,
            'fileNames3': unique_filename
        })

    except Exception as e:
        print(f"Unhandled exception in /merge-clips route:")
        traceback.print_exc()
        return jsonify({
            'error': str(e),
            'status': False
        }), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)