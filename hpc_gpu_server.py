#!/usr/bin/env python3

import rpyc
from rpyc.utils.server import ThreadedServer
import logging
import socket
import subprocess
import os
import h5py
import numpy as np
import torch
import threading
import queue
import time
from typing import Dict, Any, Optional, Tuple, List
from pathlib import Path
import hydra
from omegaconf import DictConfig

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('hpc_gpu_server.log')
    ],
    force=True
)
logger = logging.getLogger('hpc_gpu_server')
def get_gpu_info() -> Dict[str, Any]:
    """Get comprehensive GPU information using multiple methods"""
    info = {}
    
    # PyTorch GPU info
    info['cuda_available'] = torch.cuda.is_available()
    if info['cuda_available']:
        info['device_count'] = torch.cuda.device_count()
        info['current_device'] = torch.cuda.current_device()
        info['device_name'] = torch.cuda.get_device_name(0)
        
        # Get per-device info
        devices = []
        for i in range(info['device_count']):
            props = torch.cuda.get_device_properties(i)
            dev_info = {
                'name': props.name,
                'compute_capability': f"{props.major}.{props.minor}",
                'total_memory': props.total_memory,
                'memory_allocated': torch.cuda.memory_allocated(i),
                'memory_reserved': torch.cuda.memory_reserved(i),
            }
            devices.append(dev_info)
        info['devices'] = devices
    
    # nvidia-smi output
    try:
        nvidia_smi = subprocess.check_output(['nvidia-smi'], stderr=subprocess.STDOUT)
        info['nvidia_smi'] = nvidia_smi.decode()
    except Exception as e:
        info['nvidia_smi_error'] = str(e)
    
    # nvcc version
    try:
        nvcc = subprocess.check_output(['nvcc', '--version'], stderr=subprocess.STDOUT)
        info['nvcc'] = nvcc.decode()
    except Exception as e:
        info['nvcc_error'] = str(e)
        
    return info

class DatasetCache:
    """Thread-safe cache for dataset chunks"""
    def __init__(self, max_size_mb: int = 1024):
        self.max_size = max_size_mb * 1024 * 1024  # Convert to bytes
        self.current_size = 0
        self._cache: Dict[str, Dict[str, np.ndarray]] = {}
        self._access_times: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()
        
    def _get_chunk_key(self, slices: Tuple[slice, ...]) -> str:
        """Convert slice objects into a cache key"""
        return str([(s.start, s.stop, s.step) for s in slices])
    
    def get(self, dataset_path: str, slices: Tuple[slice, ...]) -> Optional[np.ndarray]:
        """Get chunk from cache if available"""
        chunk_key = self._get_chunk_key(slices)
        with self._lock:
            if dataset_path in self._cache and chunk_key in self._cache[dataset_path]:
                self._access_times[dataset_path][chunk_key] = time.time()
                return self._cache[dataset_path][chunk_key]
        return None
    
    def put(self, dataset_path: str, slices: Tuple[slice, ...], data: np.ndarray):
        """Add chunk to cache, evicting old entries if needed"""
        chunk_key = self._get_chunk_key(slices)
        chunk_size = data.nbytes
        
        with self._lock:
            # Initialize dataset entries if needed
            if dataset_path not in self._cache:
                self._cache[dataset_path] = {}
                self._access_times[dataset_path] = {}
            
            # Evict old entries if needed
            while self.current_size + chunk_size > self.max_size:
                self._evict_oldest()
            
            # Add new chunk
            self._cache[dataset_path][chunk_key] = data
            self._access_times[dataset_path][chunk_key] = time.time()
            self.current_size += chunk_size
    
    def _evict_oldest(self):
        """Remove least recently used chunk"""
        oldest_time = float('inf')
        oldest_dataset = None
        oldest_chunk = None
        
        for dataset_path, chunks in self._access_times.items():
            for chunk_key, access_time in chunks.items():
                if access_time < oldest_time:
                    oldest_time = access_time
                    oldest_dataset = dataset_path
                    oldest_chunk = chunk_key
        
        if oldest_dataset and oldest_chunk:
            chunk_size = self._cache[oldest_dataset][oldest_chunk].nbytes
            del self._cache[oldest_dataset][oldest_chunk]
            del self._access_times[oldest_dataset][oldest_chunk]
            self.current_size -= chunk_size
            
            # Clean up empty dataset entries
            if not self._cache[oldest_dataset]:
                del self._cache[oldest_dataset]
                del self._access_times[oldest_dataset]
    
    def clear(self):
        """Clear all cached data"""
        with self._lock:
            self._cache.clear()
            self._access_times.clear()
            self.current_size = 0

class H5FileManager:
    """Manages open HDF5 files with automatic cleanup"""
    def __init__(self, max_files: int = 100):
        self.max_files = max_files
        self._files: Dict[str, h5py.File] = {}
        self._access_times: Dict[str, float] = {}
        self._lock = threading.Lock()
        
    def get_file(self, file_id: str) -> Optional[h5py.File]:
        """Get open file handle, updating access time"""
        with self._lock:
            if file_id in self._files:
                self._access_times[file_id] = time.time()
                return self._files[file_id]
        return None
    
    def add_file(self, file_id: str, filepath: str, mode: str = 'r') -> h5py.File:
        """Open new file, closing oldest if needed"""
        with self._lock:
            # Close oldest file if at limit
            while len(self._files) >= self.max_files:
                oldest_id = min(self._access_times.items(), key=lambda x: x[1])[0]
                self.close_file(oldest_id)
            
            # Open new file
            h5file = h5py.File(filepath, mode)
            self._files[file_id] = h5file
            self._access_times[file_id] = time.time()
            return h5file
    
    def close_file(self, file_id: str):
        """Close file and clean up"""
        with self._lock:
            if file_id in self._files:
                self._files[file_id].close()
                del self._files[file_id]
                del self._access_times[file_id]
    
    def close_all(self):
        """Close all open files"""
        with self._lock:
            for h5file in self._files.values():
                h5file.close()
            self._files.clear()
            self._access_times.clear()

class H5StreamService(rpyc.Service):
    """RPyC service for streaming HDF5 data"""
    
    def __init__(self):
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        logger.info(f"Using device: {self.device}")
        
        # Initialize managers
        self.file_manager = H5FileManager()
        self.cache = DatasetCache()
        
        # Log initialization
        gpu_info = get_gpu_info()
        if gpu_info['cuda_available']:
            logger.info(f"GPU available: {gpu_info['device_name']}")
        else:
            logger.warning("No GPU available")
    
    def on_connect(self, conn):
        logger.info(f"New connection from {conn._config['endpoints'][1]}")
    
    def on_disconnect(self, conn):
        logger.info(f"Client disconnected: {conn._config['endpoints'][1]}")
    
    def exposed_check_connection(self):
        """Simple connection test"""
        return True
    
    def exposed_get_gpu_info(self):
        """Get GPU status information"""
        return get_gpu_info()
    
    def exposed_check_file_exists(self, filepath: str) -> bool:
        """Check if an h5 file exists and is readable"""
        try:
            return os.path.isfile(filepath) and h5py.is_hdf5(filepath)
        except Exception as e:
            logger.error(f"Error checking file {filepath}: {str(e)}")
            return False
    
    def exposed_open_h5(self, filepath: str) -> Dict[str, Any]:
        """Open an h5 file and return its metadata"""
        try:
            file_id = str(hash(filepath))
            h5file = self.file_manager.add_file(file_id, filepath, mode = 'r+')
            
            # Get attributes
            attrs = dict(h5file.attrs)
            
            # Get structure without loading data
            def get_structure(group):
                structure = {}
                for key, item in group.items():
                    if isinstance(item, h5py.Group):
                        structure[key] = get_structure(item)
                    else:
                        structure[key] = {
                            'shape': item.shape,
                            'dtype': str(item.dtype),
                            'chunks': item.chunks
                        }
                return structure
            
            return {
                'file_id': file_id,
                'attributes': attrs,
                'structure': get_structure(h5file)
            }
            
        except Exception as e:
            logger.error(f"Error opening {filepath}: {str(e)}")
            raise
    
    def exposed_close_h5(self, file_id: str):
        """Close an open h5 file"""
        try:
            self.file_manager.close_file(file_id)
            return True
        except Exception as e:
            logger.error(f"Error closing file {file_id}: {str(e)}")
            return False
 
    def exposed_create_dataset(self, file_id: str, path: str, shape: Tuple[int, ...], dtype: str, compression: str):
        """
        Create a dataset in the HDF5 file if it does not already exist.

        Args:
            file_id: The ID of the file.
            path: Path of the dataset.
            shape: Shape of the dataset.
            dtype: Data type of the dataset.
            compression: Compression method (e.g., 'gzip').

        Raises:
            ValueError: If the dataset already exists but has a different shape or dtype.
        """
        try:
            h5file = self.file_manager.get_file(file_id)
            if path in h5file:
                # If dataset exists, validate its shape and dtype
                dataset = h5file[path]
                if dataset.shape != shape or str(dataset.dtype) != dtype:
                    raise ValueError(
                        f"Dataset {path} already exists with shape {dataset.shape} "
                        f"and dtype {dataset.dtype}, which does not match the requested shape {shape} and dtype {dtype}."
                    )
                print(h5file[path][:3])
                del h5file[path]
            # Create the dataset if it does not exist
            h5file.create_dataset(path, shape=shape, dtype=dtype, compression=compression)
            logger.info(f"Dataset {path} created with shape {shape} and dtype {dtype}.")
        except Exception as e:
            logger.error(f"Error creating dataset {path}: {str(e)}")
            raise
    
    def exposed_write_dataset(self, file_id: str, path: str, data: List[List[List[float]]]):
        try:
            h5file = self.file_manager.get_file(file_id)
            data = np.array(data).astype(np.float32)
            print(f"Data Shape: {data.shape}, Data Type: {data.dtype}")
            h5file[path][...] = data
        except Exception as e:
            logger.error(f"Error writing to dataset {path}: {str(e)}")
            raise
        
    def exposed_update_dataset(self, file_id, dataset_path, patch_data):
        """Apply updates to the specified dataset."""
        try:
            h5file = self.file_manager.get_file(file_id)
            dataset = h5file[dataset_path]
            frame, neuron, coord = patch_data["frame"], patch_data["neuron"], patch_data["coord"]

            # Update only the necessary part
            if coord is None:
                dataset[frame, neuron, :] = np.nan
            else:
                dataset[frame, neuron, :] = coord

            logger.info(f"Updated dataset at frame {frame}, neuron {neuron} with {coord}")
            h5file.flush()
        except Exception as e:
            logger.error(f"Error updating dataset: {str(e)}")

    def exposed_get_dataset_info(self, file_id: str, path: str) -> Optional[Dict[str, Any]]:
        """Get dataset metadata without loading data"""
        try:
            h5file = self.file_manager.get_file(file_id)
            if h5file is None or path not in h5file:
                return None
                
            dataset = h5file[path]
            return {
                'shape': dataset.shape,
                'dtype': str(dataset.dtype),
                'chunks': dataset.chunks
            }
        except Exception as e:
            logger.error(f"Error getting dataset info {path}: {str(e)}")
            raise
    
    def exposed_get_helper_keys(self, file_id: str, prefix: str = "helper_") -> List[str]:
        """
        Fetch keys starting with a specific prefix (default: 'helper_') from the file structure.

        Args:
            file_id: ID of the file to retrieve keys from.
            prefix: Prefix to filter keys (default: 'helper_').

        Returns:
            List of keys starting with the specified prefix.
        """
        try:
            h5file = self.file_manager.get_file(file_id)
            if h5file is None:
                raise ValueError(f"File ID {file_id} not found.")

            # Get keys and filter by prefix
            keys = [key for key in h5file.keys() if key.startswith(prefix)]
            return keys
        except Exception as e:
            logger.error(f"Error fetching keys with prefix '{prefix}': {str(e)}")
            return []

    def exposed_get_dataset_chunk(self, file_id: str, path: str, slice_info: List[Tuple[int, int, int]]) -> Optional[np.ndarray]:
        """
        Get a specific chunk of data from an HDF5 dataset.

        Args:
            file_id: The ID of the file.
            path: The dataset path within the HDF5 file.
            slice_info: A list of slices (start, stop, step) for each dimension.

        Returns:
            The requested chunk as a NumPy array, or None if unavailable.
        """
        try:
            # Convert slice info to proper slices
            slices = tuple(slice(*s) for s in slice_info)
            
            # Check cache first
            cached_data = self.cache.get(path, slices)
            if cached_data is not None:
                logger.info(f"Cache hit for path: {path}, slices: {slice_info}")
                return cached_data

            # Retrieve file handle
            h5file = self.file_manager.get_file(file_id)
            if h5file is None:
                logger.error(f"File not found or closed: {file_id}")
                return None
            
            if path not in h5file:
                logger.warning(f"Dataset path not found in file: {path}")
                return None

            # Retrieve dataset
            dataset = h5file[path]
            chunk_data = dataset[slices]

            # Ensure data is a NumPy array with consistent dtype
            chunk_data = np.asarray(chunk_data, dtype=dataset.dtype)

            # Cache the chunk if it's small enough (< 10MB)
            if chunk_data.nbytes < 10 * 1024 * 1024:
                try:
                    self.cache.put(path, slices, chunk_data)
                    logger.info(f"Cached chunk for path: {path}, slices: {slice_info}")
                except Exception as e:
                    logger.warning(f"Failed to cache chunk: {e}")

            return chunk_data

        except Exception as e:
            logger.error(f"Error retrieving chunk from file {file_id}, path {path}, slices {slice_info}: {e}")
            return None

    
    def _cleanup(self):
        """Clean up resources"""
        self.file_manager.close_all()
        self.cache.clear()
@hydra.main(version_base=None, config_path="conf", config_name="config")
def app(cfg: DictConfig):
    """Main function to start the server"""
    # Set up server
    port = cfg.server.port
    host = '0.0.0.0'
    
    # Get hostname for client connection info
    node_hostname = socket.gethostname()
    logger.info(f"Starting HPC GPU server on {host}:{port}")
    logger.info(f"Connect using hostname: {node_hostname}")
    
    # Log environment info
    gpu_info = get_gpu_info()
    logger.info(f"\nGPU Information:")
    if gpu_info['cuda_available']:
        logger.info(f"CUDA Device: {gpu_info['device_name']}")
        logger.info(f"Device Count: {gpu_info['device_count']}")
        for i, dev in enumerate(gpu_info['devices']):
            logger.info(f"\nDevice {i}:")
            logger.info(f"  Name: {dev['name']}")
            logger.info(f"  Compute Capability: {dev['compute_capability']}")
            logger.info(f"  Total Memory: {dev['total_memory'] / 1024**2:.1f} MB")
    else:
        logger.warning("No GPU available")
    
    # Start server
    server = ThreadedServer(
        H5StreamService,
        hostname=host,
        port=port,
        protocol_config={
            'allow_public_attrs': True,
            'allow_all_attrs': True,
            'sync_request_timeout': 3600,  # 1 hour timeout
            'allow_pickle': True
        }
    )
    
    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    finally:
        # Clean up
        if hasattr(server.service, '_cleanup'):
            server.service._cleanup()

if __name__ == "__main__":
    app()