# PyWren on IBM Virtual Private Cloud service (VPC)

The IBM VPC client is a component for PyWren's docker executor using a remote host. It is used for start and stop VM instances over an IBM VPC automatically.

### Setup

1. Follow [IBM VPC setup](https://cloud.ibm.com/docs/vpc?topic=vpc-creating-a-vpc-using-cli) tutorial to create a VPC, create subnets in one or more regions and to attach a public gateway (using the IBM Cloud CLI)

2. Create a SSH key in [IBM VPC SSH keys UI](https://cloud.ibm.com/vpc-ext/compute/sshKeys)

3. Create a virtual server instance (VM) in [IBM VPC virtual server instances UI](https://cloud.ibm.com/vpc-ext/compute/vs) with CPUs and RAM needed for your application

4. Reserve and associate a floating IP address in [IBM VPC floating IPs UI](https://cloud.ibm.com/vpc-ext/network/floatingIPs) to be used for the virtual server instance

### Configuration

1. Get your IBM IAM API key, you can create new keys [here](https://cloud.ibm.com/iam/apikeys)

2. Get the floating IP address of your virtual server instance which can be found [here](https://cloud.ibm.com/vpc-ext/network/floatingIPs)

3. Get the endpoint of your subnet region, endpoint URLs list can be found [here](https://cloud.ibm.com/apidocs/vpc#endpoint-url)

4. Get the virtual server instance ID by selecting on your instance in [IBM VPC virtual server instances UI](https://cloud.ibm.com/vpc-ext/compute/vs) and then extracting from the instance's details

5. Edit your pywren config file and add the relevant keys:

   ```yaml
   pywren:
       compute_backend: docker
       storage_backend: ibm_cos
   
   ibm:
       iam_api_key: <iam-api-key>
   
   docker:
       host: <floating-ip-address>
       ssh_user: root
       ssh_password: <passphrase> # OPTIONAL, will use '' if not provided
       ssh_key_filename: <private-ssh-key-path> # OPTIONAL, will use the default path if not provided
       remote_client: ibm_vpc
   
   ibm_vpc:
       endpoint: <endpoint>
       instance_id: <instance-id>
       version: dd-mm-yyyy # OPTIONAL, will use today's date if not provided
       generation: 1/2 # OPTIONAL, will use 2 if not provided
   ```

   - **version**: use for specifying IBM VPC production application version date, it is recommended to configure it statically
   - **generation**: use for specifying IBM VPC environment compute generation, see [Comparing compute generations in VPC](https://cloud.ibm.com/docs/cloud-infrastructure?topic=cloud-infrastructure-compare-vpc-vpcoc) for additional information

### Verify

1. Run the following script to test if PyWren on IBM VPC is working properly:

   ```python
   import pywren_ibm_cloud as pywren

   def hello_world(name):
       return 'Hello {}!'.format(name)

   if __name__ == '__main__':
       exec = pywren.docker_executor()
       exec.call_async(hello_world, 'World')
       print("Response from function: ", exec.get_result())
   ```
