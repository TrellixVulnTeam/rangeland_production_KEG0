"""Module to quickly and completely hash a file."""

import threading
import hashlib
import Queue
import os
import time

def _read_file(file_path, file_buffer_queue, blocksize, fast_hash=False):
    """Hash `file_path` one blocksize at a time and adds to file_buffer_queue."""
    with open(file_path, 'rb') as file_to_hash:
        if fast_hash:
            #fast hash reads the first and last blocks and uses the modified
            #stamp and filesize
            buf = file_to_hash.read(blocksize)
            file_buffer_queue.put(buf)
            file_size = os.path.getsize(file_path)
            if file_size - blocksize > 0:
                file_to_hash.seek(file_size - blocksize)
                buf = file_to_hash.read(blocksize)
            file_buffer_queue.put(buf)
            file_buffer_queue.put(os.path.basename(file_path))
            file_buffer_queue.put(str(file_size))
            file_buffer_queue.put(time.ctime(os.path.getmtime(file_path)))
        else:
            buf = file_to_hash.read(blocksize)
            while len(buf) > 0:
                file_buffer_queue.put(buf)
                buf = file_to_hash.read(blocksize)
    file_buffer_queue.put('STOP')

def _hash_blocks(file_buffer_queue):
    """Processes the file_buffer_queue one buf at a time and adds to current
        hash"""
    hasher = hashlib.sha1()
    for row_buffer in iter(file_buffer_queue.get, "STOP"):
        hasher.update(row_buffer)
    file_buffer_queue.put(hasher.hexdigest()[:16])

def hashfile(file_path, blocksize=2**20, fast_hash=False):
    """Memory efficient and threaded function to return a hash since this
        operation is IO bound"""

    file_buffer_queue = Queue.Queue(100)
    read_file_process = threading.Thread(
        target=_read_file, args=(
            file_path, file_buffer_queue, blocksize, fast_hash))
    read_file_process.start()
    hash_blocks_process = threading.Thread(
        target=_hash_blocks, args=(file_buffer_queue,))
    hash_blocks_process.start()
    read_file_process.join()
    hash_blocks_process.join()
    file_hash = file_buffer_queue.get()
    if fast_hash:
        # this appends something so that a small file will have a different
        # hash whether it's fast hash or slow hash
        file_hash += '_fast_hash'
    return file_hash
