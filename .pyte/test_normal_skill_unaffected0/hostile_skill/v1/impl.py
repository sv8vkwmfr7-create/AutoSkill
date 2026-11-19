import hashlib


def run(params):
    return hashlib.md5(params['path'].encode()).hexdigest()
