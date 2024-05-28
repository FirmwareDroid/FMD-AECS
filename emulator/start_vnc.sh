#!/bin/bash


main() {
    run_vnc_server
    printf "VNC server has been started\n"
}

run_vnc_server() {
    mkdir -p ~/.vnc && \
    echo "your_password" | vncpasswd -f > ~/.vnc/passwd && \
    chmod 600 ~/.vnc/passwd
    tightvncserver -geometry 1280x800 :1
    wait $!
}


control_c() {
    echo ""
    exit
}

trap control_c SIGINT SIGTERM SIGHUP

main

exit