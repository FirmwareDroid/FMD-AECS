# FMD Android Emulator Connector Service

This repository contains all modules necessary to start multiple Android emulators
in docker and expose their gRPC API via an envoy reverse proxy. In addition, to support
WebRTC, a coturn server is started by default.


### Quick-Start

1. Create startup files. Assure that your firmware images are in the current working directory under
`./aosp_images/12/`. 
    ```
    create_startup_scripts.py -n 2 -v ./aosp_images/12/
    ```
2. Start the containers with docker-compose:
    ```
    docker-compose up
    ```

### Usage
To setup the files for the connection service start the `create_startup_scripts.py` 
in python: `python3 ./create_startup_scripts.py`. This
will generate the default setup for the service with one Android docker emulator. 
To scale the number of containers use the parameter `-n x`, 
where x is the number of instances to generate:

```
usage: create_startup_scripts.py [-h] [-n INSTANCE_COUNT] [-v [VOLUME_PATH]] [-g GRPC_START_PORT] [-a ADB_START_PORT]

Creates necessary files to startup the proxy service. A new docker-compose YAML file will be written to the current working directory.

optional arguments:
  -h, --help            show this help message and exit
  -n INSTANCE_COUNT, --instance-count INSTANCE_COUNT
                        Number of emulator instances to create.
  -v [VOLUME_PATH], --volume-path [VOLUME_PATH]
                        Path to root of the Android images directory. Every image needs to be stored in a numbered subfolder. For instance, subfolder '1' will be used by the first emulator and the subfolder named
                        '2' by the second.
  -g GRPC_START_PORT, --grpc-start-port GRPC_START_PORT
                        Starting port for the grpc service.
  -a ADB_START_PORT, --adb-start-port ADB_START_PORT
                        Starting port for the adb service.

```

The `create_startup_scripts.py` will generate a `docker-compose.yaml` file and an `envoy.yaml` config file. After the
files have been generated you can start the service with `docker-compose up`.










