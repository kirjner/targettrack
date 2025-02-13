# TargetTrack

TargetTrack is a user-friendly graphical interface designed for annotating 3D recordings of *C. elegans* and tracking neuronal activity. This fork enhances the original functionality by enabling the GUI to run on a local client machine while delegating backend processing to a remote server, optimizing performance and resource utilization.

## Features

- **Local GUI Execution**: Run the graphical interface on your local machine for responsive interaction.
- **Remote Backend Processing**: Offload computationally intensive tasks to a remote server, leveraging its processing power.
- **Secure SSH Tunneling**: Establish secure connections between the client and server to ensure data integrity and confidentiality.

## Installation

### Prerequisites

**Local Machine**:

- Python 3.8 or higher
- Conda or Mamba for environment management
- SSH access to the remote server

**Remote Server**:

- Python 3.8 or higher
- Necessary computational resources for backend processing

### Setup Steps

1. **Clone the Repository**:

   ```bash
   git clone https://github.com/kirjner/targettrack.git
   cd targettrack
   ```

2. **Set Up the Conda Environment on the Local Machine**:

   ```bash
   mamba env create -f gui_env.yaml
   mamba activate targettrack_gui
   ```

3. **Set Up the Conda Environment on the Remote Server**:

   Ensure the remote server has the necessary dependencies installed. You can use the provided `server_env.yaml` file to set up the environment:

   ```bash
   mamba env create -f server_env.yaml
   mamba activate targettrack_server
   conda install -c conda-forge jupyterlab
   ```

   Before running the above, you may need to:

   - **Load Miniforge Module**:

     ```bash
     module load openmind8/miniforge/24.3.0-0
     mamba init
     ```

   - **Allocate Resources on the Cluster**:

     ```bash
     tmux new -s targettrack
     salloc -t 4:00:00 --gres=gpu:tesla-v100:1
     ```

## Usage

1. **Start the Remote Server**:

   On the remote server, ensure the `targettrack_server` environment is active, navigate to the `targettrack` directory, and run:

   ```bash
   python hpc_gpu_server.py
   ```

   If that command runs succesfully, an output like below printed to the command line:
   ```bash
   YYYY-MM-DD HH:MM:SS,XXX - hpc_gpu_server - INFO - Starting HPC GPU server on 0.0.0.0:18861
   YYYY-MM-DD HH:MM:SS,XXX - hpc_gpu_server - INFO - Connect using hostname: node2810
   YYYY-MM-DD HH:MM:SS,XXX - hpc_gpu_server - INFO -
   GPU Information:
   YYYY-MM-DD HH:MM:SS,XXX - hpc_gpu_server - INFO - CUDA Device: NVIDIA L4
   YYYY-MM-DD HH:MM:SS,XXX - hpc_gpu_server - INFO - Device Count: 1
   YYYY-MM-DD HH:MM:SS,XXX - hpc_gpu_server - INFO -
   Device 0:
   YYYY-MM-DD HH:MM:SS,XXX - hpc_gpu_server - INFO -   Name: NVIDIA L4
   YYYY-MM-DD HH:MM:SS,XXX - hpc_gpu_server - INFO -   Compute Capability: 8.9
   YYYY-MM-DD HH:MM:SS,XXX - hpc_gpu_server - INFO -   Total Memory: 22478.3 MB
   YYYY-MM-DD HH:MM:SS,XXX - H5STREAM/18861 - INFO - server started on [0.0.0.0]:18861
   ```

   In the example output above the `hostname=node2810` and the `port=18861`. 

2. **Establish an SSH Tunnel from the Local Machine**:

   Set up port forwarding to securely connect to the remote server:

   ```bash
   ssh -v -N -L ${port}:{node}:${port} $(whoami)@remote_server_address
   ```

   Replace `remote_server_address` with the actual address of your remote server  (e.g `orcd-login001.mit.edu`).

3. **Launch the GUI on the Local Machine**:

   With the SSH tunnel active, run the GUI launcher:

   ```bash
   python gui_launcher.py /path/to/your/datafile.h5
   ```

   Replace `/path/to/your/datafile.h5` with the path to your HDF5 data file.

   If your data is stored on a remote server, you may need to copy it to your local machine:

   ```bash
   scp /orcd/data/edboyden/002/microscopy_data/targettrack_data/unlabeled_worm_head_example.h5 ~/om2/targettrack/data
   ```

## Acknowledgements

This project builds upon the original [TargetTrack](https://github.com/rahi-lab/targettrack) developed by the Rahi Lab.

For detailed information on system requirements and dependencies, please refer to the original [TargetTrack README](https://github.com/kirjner/targettrack/blob/main/TargetTrack%20README.md).
