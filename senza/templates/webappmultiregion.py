'''
HTTP app with auto scaling, ELB and multi-region DNS
'''

import re
from clickclick import warning, error, info, Action
from senza.utils import pystache_render
from ._helper import prompt, choice, confirm, check_security_group, check_iam_role, get_mint_bucket_name, check_value

TEMPLATE = '''
# basic information for generating and executing this definition
SenzaInfo:
  StackName: {{application_id}}
  Parameters:
    - ImageVersion:
        Description: "Docker image version of {{ application_id }}."

# a list of senza components to apply to the definition
SenzaComponents:

  # this basic configuration is required for the other components
  - Configuration:
      Type: Senza::StupsAutoConfiguration # auto-detect network setup

  # will create a launch configuration and auto scaling group with scaling triggers
  - AppServer:
      Type: Senza::TaupageAutoScalingGroup
      InstanceType: {{ instance_type }}
      SecurityGroups:
        - Stack: {{application_id}}-base-resources
          LogicalId: {{application_id_camel}}SecurityGroup
      IamRoles:
        - Stack: {{application_id}}-base-resources
          LogicalId: {{application_id_camel}}Role
      ElasticLoadBalancer: AppLoadBalancer
      AssociatePublicIpAddress: false # change for standalone deployment in default VPC
      TaupageConfig:
        application_version: "{{=<% %>=}}{{Arguments.ImageVersion}}<%={{ }}=%>"
        runtime: Docker
        source: "{{ docker_image }}:{{=<% %>=}}{{Arguments.ImageVersion}}<%={{ }}=%>"
        health_check_path: {{http_health_check_path}}
        ports:
          {{http_port}}: {{http_port}}
        {{#mint_bucket}}
        mint_bucket: "{{ mint_bucket }}"
        {{/mint_bucket}}

  # creates an ELB entry and Route53 domains to this ELB
  - AppLoadBalancer:
      Type: Senza::WeightedDnsElasticLoadBalancer
      HTTPPort: {{http_port}}
      HealthCheckPath: {{http_health_check_path}}
      SecurityGroups:
        - Stack: {{application_id}}-base-resources
          LogicalId: {{application_id_camel}}LoadBalancerSecurityGroup
      Scheme: {{loadbalancer_scheme}}
      Domains:
        MainDomain:
          Type: weighted
          Zone: "{{hosted_zone}}"
          Subdomain: {{application_id}}-{{=<% %>=}}{{AccountInfo.Region}}<%={{ }}=%>
        VersionDomain:
          Type: standalone
          Zone: "{{hosted_zone}}"
          Subdomain: {{application_id}}-{{=<% %>=}}{{AccountInfo.Region}}-{{SenzaInfo.StackVersion}}<%={{ }}=%>
'''

BASE_TEMPLATE = '''
SenzaInfo:
  StackName: {{application_id}}-base

Resources:
  {{application_id_camel}}RegionRecord:
    Type: AWS::Route53::RecordSet
    Properties:
      Type: CNAME
      TTL: 20
      HostedZoneName: "{{hosted_zone}}"
      Name: "{{application_id}}.{{hosted_zone}}"
      Region: "{{=<% %>=}}{{AccountInfo.Region}}<%={{ }}=%>"
      SetIdentifier: "{{application_id}}-{{=<% %>=}}{{AccountInfo.Region}}<%={{ }}=%>"
      ResourceRecords:
        - "{{application_id}}-{{=<% %>=}}{{AccountInfo.Region}}<%={{ }}=%>.{{hosted_zone}}"
  {{application_id_camel}}Role:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
        - Effect: Allow
          Principal:
            Service: ec2.amazonaws.com
          Action: sts:AssumeRole
      Path: /
      {{#mint_bucket}}
      Policies:
      - PolicyName: AllowMintRead
        PolicyDocument:
          Version: "2012-10-17"
          Statement:
          - Effect: Allow
            Action: "s3:GetObject"
            Resource: ["arn:aws:s3:::{{ mint_bucket }}/{{application_id}}/*"]
      {{/mint_bucket}}
  {{application_id_camel}}SecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: "app-{{application_id}}"
      Tags:
        - Key: Name
          Value: app-{{application_id}}
      SecurityGroupIngress:
        - IpProtocol: tcp
          FromPort: 22
          ToPort: 22
          CidrIp: "0.0.0.0/0"
        - IpProtocol: tcp
          FromPort: 8080
          ToPort: 8080
          CidrIp: "0.0.0.0/0"
        - IpProtocol: tcp
          FromPort: 9100
          ToPort: 9100
          CidrIp: "0.0.0.0/0"
  {{application_id_camel}}LoadBalancerSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: "app-{{application_id}}-lb"
      Tags:
        - Key: Name
          Value: app-{{application_id}}-lb
      SecurityGroupIngress:
        - IpProtocol: tcp
          FromPort: 443
          ToPort: 443
          CidrIp: "0.0.0.0/0"
'''


def gather_user_variables(variables, region, account_info):
    # maximal 32 characters because of the loadbalancer-name
    prompt(variables, 'application_id', 'Application ID', default='hello-world',
           value_proc=check_value(60, '^[a-zA-Z][-a-zA-Z0-9]*$'))
    prompt(variables, 'docker_image', 'Docker image without tag/version (e.g. "pierone.example.org/myteam/myapp")',
           default='stups/hello-world')
    prompt(variables, 'http_port', 'HTTP port', default=8080, type=int)
    prompt(variables, 'http_health_check_path', 'HTTP health check path', default='/')
    prompt(variables, 'instance_type', 'EC2 instance type', default='t2.micro')
    if 'pierone' in variables['docker_image'] or confirm('Did you need OAuth-Credentials from Mint?'):
        prompt(variables, 'mint_bucket', 'Mint S3 bucket name', default=lambda: get_mint_bucket_name(region))
    else:
        variables['mint_bucket'] = None
    choice(variables, 'loadbalancer_scheme',
           prompt='Please select the load balancer scheme',
           options=[('internal',
                     'internal: only accessible from the own VPC'),
                    ('internet-facing',
                     'internet-facing: accessible from the public internet')],
           default='internal')

    variables['application_id_camel'] = "".join([x.title() for x in variables['application_id'].split('-')])
    
    variables['hosted_zone'] = account_info.Domain or 'example.com'
    if (variables['hosted_zone'][-1:] != '.'):
        variables['hosted_zone'] += '.'

    name = variables['definition_file']
    base_file_name = '{}-base.yaml'.format(name[:name.rfind('.') if name.rfind('.') > 0 else len(name)])
    with Action('Generating Senza base definition file {}..'.format(base_file_name)):
      base_yaml = pystache_render(BASE_TEMPLATE, variables)
      with open(base_file_name, 'w') as file:
        file.write(base_yaml)

    info('Prepare the your stacks by executing: "senza create {} resources --region {}"'.format(
      base_file_name, account_info.Region))

    return variables


def generate_definition(variables):
    definition_yaml = pystache_render(TEMPLATE, variables)
    return definition_yaml