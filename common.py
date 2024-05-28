import zipfile


def extract_zip(file_path, destination):
    with zipfile.ZipFile(file_path, 'r') as zip_ref:
        zip_ref.extractall(destination)

