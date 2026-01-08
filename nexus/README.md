
Create a .env file for Nexus Repository Manager in this directory with the following content:
```env
NEXUS_REPOSITORY_PATH=<path-to-nexus-data-directory>
NEXUS_JETTY_PATH=<path-to-nexus-jetty-directory>
```

Example:
```
NEXUS_REPOSITORY_PATH=./nexus_data
NEXUS_JETTY_PATH=./nexus_data/jetty-https.xml
```