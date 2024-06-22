import os
import argparse
import hashlib
from PIL import Image
import cv2
from difflib import SequenceMatcher

def get_file_hash(filepath, chunk_size=1024):
    hash_algo = hashlib.md5()
    with open(filepath, 'rb') as file:
        while chunk := file.read(chunk_size):
            hash_algo.update(chunk)
    return hash_algo.hexdigest()

def get_image_resolution(filepath):
    with Image.open(filepath) as img:
        return img.size

def get_video_resolution(filepath):
    video = cv2.VideoCapture(filepath)
    if not video.isOpened():
        return None
    width = int(video.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))
    return (width, height)

def get_file_similarity(file1, file2):
    with open(file1, 'rb') as f1, open(file2, 'rb') as f2:
        file1_content = f1.read()
        file2_content = f2.read()
    return SequenceMatcher(None, file1_content, file2_content).ratio()

def find_duplicates(directory_list, recursive=False, similarity_threshold=0.95):
    file_info = {}
    duplicates = []

    for directory in directory_list:
        for root, _, files in os.walk(directory):
            for file in files:
                filepath = os.path.join(root, file)
                file_hash = get_file_hash(filepath)
                file_res = None

                if file.lower().endswith(('png', 'jpg', 'jpeg', 'bmp', 'gif')):
                    file_res = get_image_resolution(filepath)
                elif file.lower().endswith(('mp4', 'avi', 'mov', 'mkv')):
                    file_res = get_video_resolution(filepath)

                file_info[filepath] = {'hash': file_hash, 'resolution': file_res}

            if not recursive:
                break

    visited_files = set()
    all_files = list(file_info.keys())
    for i, filepath in enumerate(all_files):
        info = file_info[filepath]
        for j in range(i + 1, len(all_files)):
            other_filepath = all_files[j]
            other_info = file_info[other_filepath]
            if filepath != other_filepath:
                if info['hash'] == other_info['hash']:
                    duplicates.append((filepath, other_filepath, 'Exact match'))
                elif info['resolution'] and other_info['resolution']:
                    if info['resolution'] == other_info['resolution']:
                        duplicates.append((filepath, other_filepath, 'Same resolution'))
                else:
                    similarity = get_file_similarity(filepath, other_filepath)
                    if similarity >= similarity_threshold:
                        duplicates.append((filepath, other_filepath, f'Similarity: {similarity*100:.2f}%'))

    return duplicates

def log_results(duplicates, log_file='duplicates_log.txt'):
    with open(log_file, 'w') as log:
        for dup in duplicates:
            log.write(f'{dup[0]} and {dup[1]}: {dup[2]}\n')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Find duplicate files in specified directories.')
    parser.add_argument('directories', nargs='+', help='Directories to search for duplicates')
    parser.add_argument('-r', '--recursive', action='store_true', help='Enable recursive search')
    parser.add_argument('-t', '--threshold', type=float, default=0.95, help='Similarity threshold for fuzzy matching')
    parser.add_argument('-o', '--output', type=str, default='duplicates_log.txt', help='Output log file')

    args = parser.parse_args()

    directories_to_search = args.directories
    recursive_search = args.recursive
    similarity_threshold = args.threshold
    output_log_file = args.output

    duplicates = find_duplicates(directories_to_search, recursive=recursive_search, similarity_threshold=similarity_threshold)
    log_results(duplicates, log_file=output_log_file)
    print(f'Duplicates log saved to {output_log_file}')
