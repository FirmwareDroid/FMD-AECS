#!/usr/bin/env python3

import os
import argparse
import hashlib

def compute_hash(file_path):
    """Compute SHA-256 hash of a file."""
    hash_sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_sha256.update(chunk)
    return hash_sha256.hexdigest()

def compare_folders(folder1, folder2):
    folder1_files = {
        os.path.relpath(os.path.join(root, file), start=folder1)
        for root, _, files in os.walk(folder1)
        for file in files
    }

    folder2_files = {
        os.path.relpath(os.path.join(root, file), start=folder2)
        for root, _, files in os.walk(folder2)
        for file in files
    }

    only_in_folder1 = folder1_files - folder2_files
    in_both = folder1_files & folder2_files
    differing_files = []

    for file in in_both:
        file1 = os.path.join(folder1, file)
        file2 = os.path.join(folder2, file)
        if compute_hash(file1) != compute_hash(file2):
            differing_files.append(file)

    print("✅ Files only in folder1:")
    for f in sorted(only_in_folder1):
        print(f"  {f}")

    print("\n⚠️ Files in both folders but differ:")
    for f in sorted(differing_files):
        print(f"  {f}")

def main():
    parser = argparse.ArgumentParser(
        description="Compare two folders: check which files are missing or differ."
    )
    parser.add_argument("folder1", help="Path to the first folder")
    parser.add_argument("folder2", help="Path to the second folder")
    args = parser.parse_args()

    if not os.path.isdir(args.folder1):
        print(f"Error: '{args.folder1}' is not a valid directory.")
        return

    if not os.path.isdir(args.folder2):
        print(f"Error: '{args.folder2}' is not a valid directory.")
        return

    compare_folders(args.folder1, args.folder2)

if __name__ == "__main__":
    main()
