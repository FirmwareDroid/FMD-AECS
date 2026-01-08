# CA Certificates

## Creating Custom CA Certificates

1. To create a custom CA certificate using OpenSSL, you can use the following commands:
    ```
    openssl req -new -x509 -days 365 -keyout my-ca.key -out my-ca.crt -subj "/CN=My Custom CA"
    ```
2. Convert to PEM Format
    ```
    openssl x509 -in my-ca.crt -out my-ca.pem -outform PEM
    ```
3. Generate Subject Hash (Filename)
    ```
    openssl x509 -in burp_ca_certificate.pem -subject_hash_old -noout 
    ```
   
## Adding Custom CA Certificates to AOSP Builds
- Copy the PEM file to the appropriate directory in the AOSP source tree based on the Android version you are building.
- Rename the file to `<subject_hash>.0`.
- Set the correct permissions for the file to `644` to ensure it is readable by the system.
- Rebuild the AOSP image to include the new CA certificate.

### CA-Certificate Paths
```
# Android 14
/home/ubuntu/aosp/aosp14/external/conscrypt/apex/ca-certificates/files/
```