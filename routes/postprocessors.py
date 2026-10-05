# routes/postprocessors.py - HCL code post-processors for the generate pipeline
"""
Post-processors that fix common LLM mistakes in generated Terraform code.
Each function takes HCL code as input and returns corrected HCL code.
"""
import re
import logging

logger = logging.getLogger("terraform-agent")


def _fix_module_blocks(code):
    """Remove module blocks that reference local directories (which don't exist)."""
    if 'module "' not in code:
        return code

    result = code
    module_pattern = re.compile(r'module\s+"\w+"\s*\{')
    removed = []
    while True:
        m = module_pattern.search(result)
        if not m:
            break
        start = m.start()
        pos = m.end()
        depth = 1
        while pos < len(result) and depth > 0:
            if result[pos] == '{':
                depth += 1
            elif result[pos] == '}':
                depth -= 1
            pos += 1
        module_text = result[start:pos]
        module_name = re.search(r'module\s+"(\w+)"', module_text).group(1)
        removed.append(module_name)
        result = result[:start] + result[pos:]

    if removed:
        logger.info("[Generate] Post-fix: removed module blocks: %s", ", ".join(removed))
        result = re.sub(r'\n{3,}', '\n\n', result)

    return result


def _fix_eip_vpc_deprecated(code):
    """Replace deprecated `vpc = true` with `domain = "vpc"` in aws_eip resources."""
    if 'aws_eip' not in code:
        return code
    if not re.search(r'\bvpc\s*=\s*true\b', code):
        return code

    logger.info("[Generate] Post-fix: replacing deprecated 'vpc = true' with 'domain = \"vpc\"' in aws_eip")

    eip_pattern = re.compile(r'resource\s+"aws_eip"\s+"\w+"\s*\{')
    result = code
    offset = 0
    for m in eip_pattern.finditer(code):
        start = m.start() + offset
        pos = m.end() + offset
        depth = 1
        while pos < len(result) and depth > 0:
            if result[pos] == '{':
                depth += 1
            elif result[pos] == '}':
                depth -= 1
            pos += 1
        block = result[start:pos]

        if 'vpc' not in block:
            continue

        new_block = block
        has_domain = bool(re.search(r'\bdomain\s*=', new_block))

        if has_domain:
            new_block = re.sub(r'\s*vpc\s*=\s*true\s*\n?', '\n', new_block)
        else:
            new_block = re.sub(r'\bvpc\s*=\s*true\b', 'domain = "vpc"', new_block)

        if new_block != block:
            result = result[:start] + new_block + result[pos:]
            offset += len(new_block) - len(block)

    return result


def _fix_cloudwatch_asg_dimensions(code):
    """Fix CloudWatch alarms that reference aws_autoscaling_group.*.instance_id."""
    if 'aws_cloudwatch_metric_alarm' not in code or 'aws_autoscaling_group' not in code:
        return code

    pattern = r'InstanceId\s*=\s*aws_autoscaling_group\.(\w+)\.instance_id'
    if re.search(pattern, code):
        logger.info("[Generate] Post-fix: replacing invalid ASG instance_id with AutoScalingGroupName")
        code = re.sub(pattern, r'AutoScalingGroupName = aws_autoscaling_group.\1.name', code)
        code = re.sub(
            r'(namespace\s*=\s*)"AWS/EC2"(\s*\n[^}]*AutoScalingGroupName\s*=\s*aws_autoscaling_group)',
            r'\1"AWS/AutoScaling"\2', code, flags=re.DOTALL
        )
    return code


def _fix_asg_target_group_attachment(code):
    """Replace aws_lb_target_group_attachment referencing ASG instances with aws_autoscaling_attachment."""
    if 'aws_lb_target_group_attachment' not in code or 'aws_autoscaling_group' not in code:
        return code

    tga_pattern = re.compile(r'resource\s+"aws_lb_target_group_attachment"\s+"(\w+)"\s*\{')
    for m in tga_pattern.finditer(code):
        start = m.start()
        pos = m.end()
        depth = 1
        while pos < len(code) and depth > 0:
            if code[pos] == '{':
                depth += 1
            elif code[pos] == '}':
                depth -= 1
            pos += 1
        block = code[start:pos]

        if 'aws_autoscaling_group.' in block and '.instances' in block:
            res_name = m.group(1)
            tg_match = re.search(r'target_group_arn\s*=\s*(\S+)', block)
            asg_match = re.search(r'aws_autoscaling_group\.(\w+)\.instances', block)

            if tg_match and asg_match:
                tg_ref = tg_match.group(1)
                asg_name = asg_match.group(1)
                replacement = (
                    f'resource "aws_autoscaling_attachment" "{res_name}" {{\n'
                    f'  autoscaling_group_name = aws_autoscaling_group.{asg_name}.name\n'
                    f'  lb_target_group_arn    = {tg_ref}\n'
                    f'}}'
                )
                code = code[:start] + replacement + code[pos:]
                logger.info("[Generate] Post-fix: replaced target_group_attachment with autoscaling_attachment for ASG %s", asg_name)
                break
    return code


def _fix_launch_template_sg(code):
    """Fix security_group_ids in aws_launch_template AND aws_instance."""
    if 'security_group' not in code:
        return code

    target_types = ['aws_launch_template', 'aws_instance']
    for rtype in target_types:
        pattern = re.compile(r'resource\s+"' + re.escape(rtype) + r'"\s+"\w+"\s*\{')
        for m in pattern.finditer(code):
            start = m.start()
            pos = m.end()
            depth = 1
            while pos < len(code) and depth > 0:
                if code[pos] == '{':
                    depth += 1
                elif code[pos] == '}':
                    depth -= 1
                pos += 1
            block = code[start:pos]

            if 'security_group_ids' in block and 'vpc_security_group_ids' not in block:
                fixed_block = re.sub(r'\bsecurity_group_ids\b', 'vpc_security_group_ids', block)
                if fixed_block != block:
                    code = code[:start] + fixed_block + code[pos:]
                    logger.info("[Generate] Post-fix: renamed security_group_ids to vpc_security_group_ids in %s", rtype)
                    break

            if re.search(r'^\s+security_groups\s*=', block, re.MULTILINE) and 'vpc_security_group_ids' not in block:
                lines = block.split('\n')
                in_sub_block = 0
                fixed_lines = []
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith('network_interfaces') or stripped.startswith('network_interface'):
                        in_sub_block += 1
                    if '}' in stripped and in_sub_block > 0:
                        in_sub_block -= 1
                    if in_sub_block == 0 and re.match(r'^(\s+)security_groups(\s*=)', line):
                        line = re.sub(r'\bsecurity_groups\b', 'vpc_security_group_ids', line)
                        logger.info("[Generate] Post-fix: renamed security_groups to vpc_security_group_ids in %s", rtype)
                    fixed_lines.append(line)
                fixed_block = '\n'.join(fixed_lines)
                if fixed_block != block:
                    code = code[:start] + fixed_block + code[pos:]
                    break
    return code


def _fix_kms_and_ebs(code):
    """Fix unnecessary aws_kms_key for aws/ebs and standalone aws_ebs_volume."""
    kms_match = re.search(r'resource\s+"aws_kms_key"\s+"(\w+)"\s*\{', code)
    if kms_match:
        kms_name = kms_match.group(1)
        kms_ref = f'aws_kms_key.{kms_name}.arn'

        refs = list(re.finditer(re.escape(kms_ref), code))
        if refs:
            all_ebs = True
            for ref in refs:
                context_start = max(0, ref.start() - 200)
                context = code[context_start:ref.start()]
                if not any(kw in context for kw in ['aws_ebs_volume', 'root_block_device', 'ebs_block_device']):
                    all_ebs = False
                    break

            if all_ebs:
                start = kms_match.start()
                pos = kms_match.end()
                depth = 1
                while pos < len(code) and depth > 0:
                    if code[pos] == '{':
                        depth += 1
                    elif code[pos] == '}':
                        depth -= 1
                    pos += 1
                code = code[:start] + code[pos:]

                if 'data "aws_kms_alias"' not in code:
                    provider_match = re.search(r'(provider\s+"aws"\s*\{[^}]*\}\n)', code)
                    alias_block = '\ndata "aws_kms_alias" "ebs" {\n  name = "alias/aws/ebs"\n}\n'
                    if provider_match:
                        insert_pos = provider_match.end()
                        code = code[:insert_pos] + alias_block + code[insert_pos:]
                    else:
                        code = alias_block + code

                code = code.replace(kms_ref, 'data.aws_kms_alias.ebs.target_key_arn')
                logger.info("[Generate] Post-fix: replaced aws_kms_key with aws/ebs managed key alias")

    # Remove standalone aws_ebs_volume that isn't attached
    ebs_pattern = re.compile(r'resource\s+"aws_ebs_volume"\s+"(\w+)"\s*\{')
    for m in ebs_pattern.finditer(code):
        vol_name = m.group(1)
        if 'aws_volume_attachment' not in code:
            start = m.start()
            pos = m.end()
            depth = 1
            while pos < len(code) and depth > 0:
                if code[pos] == '{':
                    depth += 1
                elif code[pos] == '}':
                    depth -= 1
                pos += 1
            code = code[:start] + code[pos:]
            logger.info("[Generate] Post-fix: removed standalone aws_ebs_volume '%s'", vol_name)

    code = re.sub(r'\n{3,}', '\n\n', code)

    # Fix S3 encryption using aws/ebs alias instead of aws/s3
    s3_enc_pattern = re.compile(
        r'(resource\s+"aws_s3_bucket_server_side_encryption_configuration"\s+"\w+"\s*\{.*?\})\s*\}',
        re.DOTALL
    )
    for m in s3_enc_pattern.finditer(code):
        block = m.group(0)
        if 'data.aws_kms_alias.ebs.target_key_arn' in block:
            if 'data "aws_kms_alias" "s3"' not in code:
                provider_match = re.search(r'(provider\s+"aws"\s*\{[^}]*\}\n)', code)
                s3_alias = '\ndata "aws_kms_alias" "s3" {\n  name = "alias/aws/s3"\n}\n'
                if provider_match:
                    code = code[:provider_match.end()] + s3_alias + code[provider_match.end():]
                else:
                    code = s3_alias + code
            new_block = block.replace(
                'data.aws_kms_alias.ebs.target_key_arn',
                'data.aws_kms_alias.s3.target_key_arn'
            )
            code = code.replace(block, new_block)
            logger.info("[Generate] Post-fix: S3 encryption now uses aws/s3 KMS alias")

    return code


def _fix_placeholder_sg(code):
    """Replace placeholder/fake security group IDs with default VPC SG data source."""
    # Fix A: data "aws_security_group" that looks up by id = var.xxx with bad default
    sg_data_pattern = re.compile(
        r'data\s+"aws_security_group"\s+"(\w+)"\s*\{[^}]*?id\s*=\s*var\.(\w+)[^}]*\}',
        re.DOTALL
    )
    for m in sg_data_pattern.finditer(code):
        data_name = m.group(1)
        var_name = m.group(2)
        var_match = re.search(
            r'variable\s+"' + re.escape(var_name) + r'"\s*\{[^}]*?default\s*=\s*"([^"]*)"',
            code, re.DOTALL
        )
        if var_match:
            default_val = var_match.group(1)
            if not re.match(r'^sg-[0-9a-f]{8,}$', default_val):
                code = re.sub(
                    r'variable\s+"' + re.escape(var_name) + r'"\s*\{[^}]*?\}\n*',
                    '', code, flags=re.DOTALL
                )
                old_data_block = m.group(0)
                if not re.search(r'data\s+"aws_vpc"\s+"default"', code):
                    provider_match = re.search(r'(provider\s+"aws"\s*\{[^}]*\}\n)', code)
                    vpc_block = '\ndata "aws_vpc" "default" {\n  default = true\n}\n'
                    if provider_match:
                        code = code[:provider_match.end()] + vpc_block + code[provider_match.end():]
                    else:
                        code = vpc_block + code
                new_data_block = (
                    f'data "aws_security_group" "{data_name}" {{\n'
                    f'  name   = "default"\n'
                    f'  vpc_id = data.aws_vpc.default.id\n'
                    f'}}'
                )
                code = code.replace(old_data_block, new_data_block)
                code = code.replace(f'var.{var_name}', f'data.aws_security_group.{data_name}.id')
                logger.info("[Generate] Post-fix: replaced placeholder SG variable '%s' with default VPC SG", var_name)

    # Fix B: literal placeholder sg- IDs
    placeholder_pattern = re.compile(
        r'(?:sg-(?:0123456789abcdef[0-9a-f]*|12345678[0-9a-f]*|[0]{8,}|[x]{8,}|[a-f0-9]{8}(?:[a-f0-9]{9})?))'
    )
    sg_refs = placeholder_pattern.findall(code)
    if not sg_refs:
        return code

    has_default_sg_data = 'data "aws_security_group" "default"' in code
    data_block = ""
    if not has_default_sg_data:
        if not bool(re.search(r'data\s+"aws_vpc"\s+"default"', code)):
            data_block += '\ndata "aws_vpc" "default" {\n  default = true\n}\n'
        data_block += '\ndata "aws_security_group" "default" {\n  name   = "default"\n  vpc_id = data.aws_vpc.default.id\n}\n'

    for sg_id in set(sg_refs):
        code = re.sub(r'default\s*=\s*"' + re.escape(sg_id) + r'"', 'default = ""', code)
        code = re.sub(r'"' + re.escape(sg_id) + r'"', 'data.aws_security_group.default.id', code)

    if re.search(r'variable\s+"security_group_id"\s*\{[^}]*default\s*=\s*""', code, re.DOTALL):
        code = re.sub(r'variable\s+"security_group_id"\s*\{[^}]*?\}\n*', '', code, flags=re.DOTALL)
        code = code.replace('var.security_group_id', 'data.aws_security_group.default.id')

    if data_block:
        provider_match = re.search(r'(provider\s+"aws"\s*\{[^}]*\}\n)', code)
        if provider_match:
            insert_pos = provider_match.end()
            code = code[:insert_pos] + data_block + code[insert_pos:]
        else:
            code = data_block + code
        logger.info("[Generate] Post-fix: replaced placeholder SG IDs with default VPC SG data source")

    return code


def _fix_asg_launch_template(code):
    """Fix aws_autoscaling_group launch_template syntax."""
    if 'aws_autoscaling_group' not in code:
        return code

    # Fix A: launch_template_id inside launch_template block -> rename to id
    changed = True
    while changed:
        changed = False
        lt_block_wrong = re.search(r'(launch_template\s*\{[^}]*?)launch_template_id(\s*=)', code, re.DOTALL)
        if lt_block_wrong:
            code = code[:lt_block_wrong.start(0)] + \
                   lt_block_wrong.group(1) + 'id' + lt_block_wrong.group(2) + \
                   code[lt_block_wrong.end(0):]
            logger.info("[Generate] Post-fix: renamed launch_template_id to id inside launch_template block")
            changed = True

    # Fix B: flat launch_template_id outside a launch_template block
    asg_blocks = list(re.finditer(r'resource\s+"aws_autoscaling_group"\s+"\w+"\s*\{', code))
    for asg_match in asg_blocks:
        start = asg_match.end()
        depth = 1
        pos = start
        while pos < len(code) and depth > 0:
            if code[pos] == '{':
                depth += 1
            elif code[pos] == '}':
                depth -= 1
            pos += 1
        asg_body = code[asg_match.start():pos]

        if 'launch_template_id' in asg_body and 'launch_template {' not in asg_body and 'launch_template{' not in asg_body:
            lt_id_match = re.search(r'\blaunch_template_id\s*=\s*([^\n]+)', asg_body)
            lt_ver_match = re.search(r'\blaunch_template_version\s*=\s*([^\n]+)', asg_body)
            if lt_id_match:
                lt_id_val = lt_id_match.group(1).strip()
                lt_ver_val = lt_ver_match.group(1).strip() if lt_ver_match else '"$Latest"'
                nested = f'launch_template {{\n    id      = {lt_id_val}\n    version = {lt_ver_val}\n  }}'
                new_body = asg_body
                new_body = re.sub(r'\s*launch_template_id\s*=\s*[^\n]+\n?', '\n', new_body, count=1)
                new_body = re.sub(r'\s*launch_template_version\s*=\s*[^\n]+\n?', '', new_body, count=1)
                insert_pos = new_body.index('{') + 1
                new_body = new_body[:insert_pos] + '\n  ' + nested + new_body[insert_pos:]
                code = code.replace(asg_body, new_body)
                logger.info("[Generate] Post-fix: converted flat launch_template_id to nested block")

    # Fix C: mixed_instances_policy
    if "mixed_instances_policy" in code:
        pattern = r'(mixed_instances_policy\s*\{[^}]*?)launch_template\s*\{([^}]*?)id\s*=\s*([^\n]+)\n([^}]*?)version\s*=\s*([^\n]+)\n\s*\}'
        m = re.search(pattern, code, re.DOTALL)
        if m:
            lt_id_ref = m.group(3).strip()
            lt_version = m.group(5).strip()
            old_block = re.search(
                r'launch_template\s*\{[^}]*?id\s*=[^}]*?version\s*=[^}]*?\}',
                code, re.DOTALL
            )
            if old_block:
                new_block = (
                    "launch_template {\n"
                    "      launch_template_specification {\n"
                    f"        launch_template_id = {lt_id_ref}\n"
                    f"        version            = {lt_version}\n"
                    "      }\n"
                    "    }"
                )
                code = code[:old_block.start()] + new_block + code[old_block.end():]
                logger.info("[Generate] Post-fix: converted to launch_template_specification in mixed_instances_policy")
    return code


def _fix_duplicate_routes(code):
    """Fix aws_route with count that creates duplicate routes in the same route table."""
    if 'aws_route' not in code or 'count' not in code:
        return code

    route_pattern2 = r'(resource\s+"aws_route"\s+"(\w+)"\s*\{[^}]*count\s*=\s*(\d+)[^}]*?)' \
                     r'(route_table_id\s*=\s*)(aws_route_table\.(\w+)\.id)' \
                     r'([^}]*\})'
    m2 = re.search(route_pattern2, code, re.DOTALL)
    if m2:
        rt_name = m2.group(6)
        rt_count = re.search(rf'resource\s+"aws_route_table"\s+"{rt_name}"\s*\{{[^}}]*count', code)
        if not rt_count:
            old_ref = m2.group(5)
            new_ref = f'element(aws_route_table.{rt_name}.*.id, count.index)'
            code = code.replace(m2.group(0), m2.group(0).replace(old_ref, new_ref))
            rt_pattern = rf'(resource\s+"aws_route_table"\s+"{rt_name}"\s*\{{)'
            rt_match = re.search(rt_pattern, code)
            if rt_match and 'count' not in code[rt_match.start():rt_match.start()+200]:
                count_val = m2.group(3)
                code = code[:rt_match.end()] + f'\n  count  = {count_val}' + code[rt_match.end():]
                logger.info("[Generate] Post-fix: added count to route_table to avoid duplicate routes")
    return code


def _fix_lb_listener_forward(code):
    """Fix ALB listener forward blocks with wrong syntax."""
    if 'aws_lb_listener' not in code:
        return code

    pattern = r'(default_action\s*\{[^}]*?type\s*=\s*"forward"\s*\n)\s*forward\s*\{\s*\n\s*target_group_arn\s*=\s*([^\n]+)\n\s*\}\s*\n'
    if re.search(pattern, code, re.DOTALL):
        code = re.sub(pattern, r'\1    target_group_arn = \2\n', code, flags=re.DOTALL)
        logger.info("[Generate] Post-fix: flattened forward { target_group_arn } in lb_listener")
    return code


def _fix_placeholder_resources(code):
    """Remove Route53 records/zones, ACM certificate references, and duplicate ALB listeners."""
    removed = []

    # 1. Remove data "aws_route53_zone" with placeholder domains
    zone_pattern = re.compile(r'data\s+"aws_route53_zone"\s+"(\w+)"\s*\{')
    for m in zone_pattern.finditer(code):
        start = m.start()
        pos = m.end()
        depth = 1
        while pos < len(code) and depth > 0:
            if code[pos] == '{': depth += 1
            elif code[pos] == '}': depth -= 1
            pos += 1
        block = code[start:pos]
        if re.search(r'name\s*=\s*"[^"]*example\.com[^"]*"', block) or \
           re.search(r'name\s*=\s*"[^"]*placeholder[^"]*"', block, re.IGNORECASE):
            zone_name = m.group(1)
            code = code[:start] + code[pos:]
            removed.append(f"data.aws_route53_zone.{zone_name}")
            code = _remove_resources_referencing(code, f"data.aws_route53_zone.{zone_name}", removed)
            break

    # 2. Remove aws_route53_record with placeholder domains
    r53_pattern = re.compile(r'resource\s+"aws_route53_record"\s+"(\w+)"\s*\{')
    changed = True
    while changed:
        changed = False
        for m in r53_pattern.finditer(code):
            start = m.start()
            pos = m.end()
            depth = 1
            while pos < len(code) and depth > 0:
                if code[pos] == '{': depth += 1
                elif code[pos] == '}': depth -= 1
                pos += 1
            block = code[start:pos]
            if re.search(r'example\.com', block) or \
               re.search(r'zone_id\s*=\s*"Z[A-Z0-9]{20,}"', block) or \
               re.search(r'data\.aws_route53_zone\.', block):
                code = code[:start] + code[pos:]
                removed.append(f"aws_route53_record.{m.group(1)}")
                changed = True
                break

    # 3. Fix HTTPS listeners with placeholder certificates
    https_listener_pattern = re.compile(r'(resource\s+"aws_lb_listener"\s+"(\w+)"\s*\{)')
    for m in https_listener_pattern.finditer(code):
        start = m.start()
        pos = m.end()
        depth = 1
        while pos < len(code) and depth > 0:
            if code[pos] == '{': depth += 1
            elif code[pos] == '}': depth -= 1
            pos += 1
        block = code[start:pos]
        cert_match = re.search(r'certificate_arn\s*=\s*"[^"]*(?:123456789012|example|placeholder|REPLACE)[^"]*"', block, re.IGNORECASE)
        if cert_match:
            new_block = re.sub(r'\s*ssl_policy\s*=\s*"[^"]*"\n?', '\n', block)
            new_block = re.sub(r'\s*certificate_arn\s*=\s*"[^"]*"[^\n]*\n?', '\n', new_block)
            new_block = re.sub(r'protocol\s*=\s*"HTTPS"', 'protocol = "HTTP"', new_block)
            if new_block != block:
                code = code[:start] + new_block + code[pos:]
                removed.append(f"certificate_arn in aws_lb_listener.{m.group(2)}")
                break

    # 4. Remove duplicate lb_listeners on the same port
    dup_pattern = re.compile(r'resource\s+"aws_lb_listener"\s+"(\w+)"\s*\{')
    listeners = []
    for m in dup_pattern.finditer(code):
        start = m.start()
        pos = m.end()
        depth = 1
        while pos < len(code) and depth > 0:
            if code[pos] == '{': depth += 1
            elif code[pos] == '}': depth -= 1
            pos += 1
        block = code[start:pos]
        port_match = re.search(r'port\s*=\s*(\d+)', block)
        port = port_match.group(1) if port_match else None
        listeners.append((m.group(1), start, pos, port))

    seen_ports = {}
    for name, start, end, port in reversed(listeners):
        if port in seen_ports:
            code = code[:start] + code[end:]
            removed.append(f"aws_lb_listener.{name} (duplicate port {port})")
        else:
            seen_ports[port] = name

    if removed:
        logger.info("[Generate] Post-fix: removed placeholder resources: %s", ", ".join(removed))
        code = re.sub(r'\n{3,}', '\n\n', code)

    return code


def _remove_resources_referencing(code, ref, removed):
    """Remove resource/data blocks that reference a given identifier."""
    pattern = re.compile(r'(resource|data)\s+"(\w+)"\s+"(\w+)"\s*\{')
    changed = True
    while changed:
        changed = False
        for m in pattern.finditer(code):
            start = m.start()
            pos = m.end()
            depth = 1
            while pos < len(code) and depth > 0:
                if code[pos] == '{': depth += 1
                elif code[pos] == '}': depth -= 1
                pos += 1
            block = code[start:pos]
            if ref in block:
                code = code[:start] + code[pos:]
                removed.append(f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
                changed = True
                break
    return code


def _fix_unnecessary_iam_roles(code):
    """Remove IAM roles/policies not referenced by any compute resource."""
    if "aws_iam_role" not in code:
        return code

    iam_roles = re.findall(r'resource\s+"aws_iam_role"\s+"(\w+)"', code)
    if not iam_roles:
        return code

    used_patterns = [
        r'aws_iam_instance_profile', r'aws_lambda_function',
        r'aws_ecs_task_definition', r'execution_role_arn',
        r'task_role_arn', r'iam_instance_profile',
    ]
    has_compute_iam_usage = any(re.search(p, code) for p in used_patterns)
    if has_compute_iam_usage:
        return code

    removed = []
    for role_name in iam_roles:
        role_pattern = rf'resource\s+"aws_iam_role"\s+"{role_name}"\s*\{{[^}}]*(?:\{{[^}}]*\}}[^}}]*)*\}}'
        if re.search(role_pattern, code, re.DOTALL):
            code = re.sub(role_pattern, '', code, count=1, flags=re.DOTALL)
            removed.append(f"aws_iam_role.{role_name}")

        policy_pattern = rf'resource\s+"aws_iam_role_policy"\s+"\w*{role_name}\w*"\s*\{{[^}}]*(?:\{{[^}}]*\}}[^}}]*)*\}}'
        if re.search(policy_pattern, code, re.DOTALL):
            code = re.sub(policy_pattern, '', code, count=1, flags=re.DOTALL)
            removed.append(f"aws_iam_role_policy (for {role_name})")

        attach_pattern = rf'resource\s+"aws_iam_role_policy_attachment"\s+"\w*{role_name}\w*"\s*\{{[^}}]*\}}'
        if re.search(attach_pattern, code, re.DOTALL):
            code = re.sub(attach_pattern, '', code, count=1, flags=re.DOTALL)
            removed.append(f"aws_iam_role_policy_attachment (for {role_name})")

    if removed:
        logger.info("[Generate] Post-fix: removed unnecessary IAM resources: %s", ", ".join(removed))
        code = re.sub(r'\n{3,}', '\n\n', code)
    return code


def _fix_ec2_hardcoded_ami(code):
    """Replace hardcoded AMI IDs with an aws_ami data source."""
    if "aws_instance" not in code and "aws_launch_template" not in code:
        return code

    ami_pattern = r'"(ami-[0-9a-f]{8,17})"'
    if not re.search(ami_pattern, code):
        return code

    if 'data "aws_ami"' in code or 'data "aws_ssm_parameter"' in code:
        return code

    logger.info("[Generate] Post-fix: replacing hardcoded AMI with aws_ami data source")

    code_lower = code.lower()
    if "ubuntu" in code_lower:
        ami_data = (
            'data "aws_ami" "latest" {\n'
            '  most_recent = true\n'
            '  owners      = ["099720109477"]\n'
            '  filter {\n'
            '    name   = "name"\n'
            '    values = ["ubuntu/images/hvm-ssd/ubuntu-*-22.04-amd64-server-*"]\n'
            '  }\n'
            '  filter {\n'
            '    name   = "virtualization-type"\n'
            '    values = ["hvm"]\n'
            '  }\n'
            '}\n'
        )
    else:
        ami_data = (
            'data "aws_ami" "latest" {\n'
            '  most_recent = true\n'
            '  owners      = ["amazon"]\n'
            '  filter {\n'
            '    name   = "name"\n'
            '    values = ["al2023-ami-*-x86_64"]\n'
            '  }\n'
            '  filter {\n'
            '    name   = "virtualization-type"\n'
            '    values = ["hvm"]\n'
            '  }\n'
            '}\n'
        )

    code = re.sub(r'ami\s*=\s*"ami-[0-9a-f]{8,17}"', 'ami = data.aws_ami.latest.id', code)
    code = re.sub(r'(default\s*=\s*)"ami-[0-9a-f]{8,17}"', r'\1"data.aws_ami.latest.id"', code)
    var_ami_pattern = r'variable\s+"([^"]*ami[^"]*?)"\s*\{[^}]*default\s*=\s*"data\.aws_ami\.latest\.id"[^}]*\}'
    for m in re.finditer(var_ami_pattern, code, re.DOTALL):
        var_name = m.group(1)
        code = code.replace(m.group(0), '')
        code = code.replace(f'var.{var_name}', 'data.aws_ami.latest.id')

    provider_end = re.search(r'(provider\s+"[^"]+"\s*\{[^}]*\})', code, re.DOTALL)
    if provider_end:
        insert_pos = provider_end.end()
        code = code[:insert_pos] + "\n\n" + ami_data + code[insert_pos:]
    else:
        code = ami_data + "\n" + code
    return code


def _fix_ec2_key_pair(code):
    """Replace fake/placeholder public keys with tls_private_key."""
    if "aws_key_pair" not in code:
        return code
    if "tls_private_key" in code:
        return code

    kp_pattern = r'resource\s+"aws_key_pair"\s+"(\w+)"\s*\{([^}]*)\}'
    matches = list(re.finditer(kp_pattern, code, re.DOTALL))
    if not matches:
        return code

    for m in matches:
        kp_name = m.group(1)
        kp_body = m.group(2)

        pk_match = re.search(r'public_key\s*=\s*"(ssh-[^"]*)"', kp_body)
        if not pk_match:
            pk_ref = re.search(r'public_key\s*=\s*(?!tls_private_key)([^\n]+)', kp_body)
            if not pk_ref or 'tls_private_key' in (pk_ref.group(1) if pk_ref else ''):
                continue
            if 'file(' not in (pk_ref.group(1) if pk_ref else ''):
                continue

        logger.info("[Generate] Post-fix: replacing hardcoded public_key in aws_key_pair.%s", kp_name)

        tls_resource_name = f"generated_{kp_name}"
        tls_block = (
            f'\nresource "tls_private_key" "{tls_resource_name}" {{\n'
            f'  algorithm = "RSA"\n'
            f'  rsa_bits  = 4096\n'
            f'}}\n'
        )

        old_pk_line = re.search(r'public_key\s*=\s*[^\n]+', kp_body)
        if old_pk_line:
            new_pk_line = f'public_key = tls_private_key.{tls_resource_name}.public_key_openssh'
            code = code.replace(old_pk_line.group(0), new_pk_line, 1)

        code = re.sub(
            rf'(resource\s+"aws_key_pair"\s+"{kp_name}")',
            tls_block + r'\1', code, count=1,
        )

        if f'output "private_key_{kp_name}"' not in code:
            output_block = (
                f'\noutput "private_key_{kp_name}" {{\n'
                f'  value     = tls_private_key.{tls_resource_name}.private_key_pem\n'
                f'  sensitive = true\n'
                f'}}\n'
            )
            code += output_block
    return code


def _default_for_var_name(name, var_type="string"):
    """Sensible Terraform default for an undeclared or default-less variable."""
    defaults_by_type = {
        "string": '"example"',
        "number": "1",
        "bool": "false",
        "list": "[]",
        "map": "{}",
    }
    default_val = defaults_by_type.get(var_type, '"example"')
    lower_name = name.lower()
    if "region" in lower_name:
        return '"us-east-1"'
    if "kms" in lower_name and "alias" in lower_name:
        return '"alias/aws/ebs"'
    if "kms" in lower_name:
        return '"alias/aws/ebs"'
    if "bucket" in lower_name:
        return '"tf-agent-bucket"'
    if "name" in lower_name:
        return f'"tf-agent-{name.replace("_", "-")}"'
    if "count" in lower_name or "size" in lower_name or "port" in lower_name:
        return "2"
    if "cidr" in lower_name:
        return '"10.0.0.0/16"'
    return default_val


def _fix_missing_defaults(code):
    """Add default values to variables that lack a default."""
    var_pattern = r'variable\s+"([^"]+)"\s*\{([^}]*)\}'

    def _add_default(match):
        name = match.group(1)
        body = match.group(2)
        if "default" in body:
            return match.group(0)

        type_m = re.search(r'type\s*=\s*(\w+)', body)
        var_type = type_m.group(1) if type_m else "string"
        default_val = _default_for_var_name(name, var_type)

        logger.info("[Generate] Post-fix: adding default=%s to variable '%s'", default_val, name)
        new_body = body.rstrip() + f"\n  default = {default_val}\n"
        return f'variable "{name}" {{{new_body}}}'

    return re.sub(var_pattern, _add_default, code, flags=re.DOTALL)


def _fix_undeclared_variables(code):
    """Declare var.X references that have no variable block in this file."""
    refs = set(re.findall(r'\bvar\.([A-Za-z_][A-Za-z0-9_]*)', code))
    decls = set(re.findall(r'variable\s+"([^"]+)"', code))
    missing = sorted(refs - decls)
    if not missing:
        return code
    blocks = []
    for name in missing:
        default_val = _default_for_var_name(name)
        blocks.append(
            f'\nvariable "{name}" {{\n  type    = string\n  default = {default_val}\n}}\n'
        )
        logger.info("[Generate] Post-fix: declared missing variable '%s' default=%s", name, default_val)
    return code.rstrip() + "".join(blocks)


def ensure_undeclared_variables_in_files(files_dict):
    """Declare var.X used in any file but missing from all files (fixes kms_alias_name etc.)."""
    if not files_dict:
        return files_dict
    combined = "\n".join(files_dict.values())
    refs = set(re.findall(r'\bvar\.([A-Za-z_][A-Za-z0-9_]*)', combined))
    decls = set(re.findall(r'variable\s+"([^"]+)"', combined))
    missing = sorted(refs - decls)
    if not missing:
        return files_dict
    blocks = []
    for name in missing:
        default_val = _default_for_var_name(name)
        blocks.append(
            f'\nvariable "{name}" {{\n  type    = string\n  default = {default_val}\n}}\n'
        )
        logger.info("[Generate] Post-fix: declared missing variable '%s' default=%s", name, default_val)
    extra = "".join(blocks)
    target = None
    for candidate in ("variables.tf", "vars.tf", "data.tf", "main.tf"):
        if candidate in files_dict:
            target = candidate
            break
    if not target:
        target = next(iter(files_dict))
    files_dict[target] = files_dict[target].rstrip() + extra + "\n"
    return files_dict


def _fix_s3_unique_names(code):
    """S3 bucket names stay as generated; uniqueness suffixes are stripped elsewhere."""
    return code


_RANDOM_ID_INTERP = re.compile(r'-?\$\{random_id\.\w+\.(?:hex|id|dec)\}')
_TRAILING_RAND_SUFFIX = re.compile(r'[-_](?:[0-9]{8}|[0-9a-fA-F]{8})$')
_NAME_LIKE_ASSIGN = re.compile(
    r'^(\s*(?:Name|name|bucket|default)\s*=\s*")([^"]*)(".*)$',
    re.MULTILINE,
)


def _remove_unreferenced_random_id(code):
    """Drop random_id resources that are no longer referenced after suffix stripping."""
    matches = list(re.finditer(r'resource\s+"random_id"\s+"(\w+)"\s*\{', code))
    for match in reversed(matches):
        name = match.group(1)
        start = match.start()
        pos = match.end()
        depth = 1
        while pos < len(code) and depth > 0:
            if code[pos] == '{':
                depth += 1
            elif code[pos] == '}':
                depth -= 1
            pos += 1
        rest = code[:start] + code[pos:]
        if f'random_id.{name}' not in rest:
            logger.info("[Generate] Post-fix: removed unused random_id.%s", name)
            code = rest
    return re.sub(r'\n{3,}', '\n\n', code)


def _strip_name_suffixes(code):
    """Remove random_id interpolations and trailing numeric/hex keys from names and buckets."""
    original = code
    code = _RANDOM_ID_INTERP.sub('', code)

    def _clean_assignment(match):
        prefix, value, suffix = match.group(1), match.group(2), match.group(3)
        cleaned = _RANDOM_ID_INTERP.sub('', value)
        cleaned = re.sub(r'^[-_]+|[-_]+$', '', cleaned)
        attr = prefix.strip().split('=')[0].strip()
        if attr in ('Name', 'name', 'bucket') or attr == 'default':
            cleaned = _TRAILING_RAND_SUFFIX.sub('', cleaned)
            cleaned = re.sub(r'^[-_]+|[-_]+$', '', cleaned)
        if not cleaned:
            cleaned = 'tf-agent-bucket' if attr == 'bucket' else 'tf-agent'
        return f'{prefix}{cleaned}{suffix}'

    code = _NAME_LIKE_ASSIGN.sub(_clean_assignment, code)
    code = _remove_unreferenced_random_id(code)
    if code != original:
        logger.info("[Generate] Post-fix: stripped random suffixes from names, tags, and buckets")
    return code


def _fix_s3_inline_blocks(code):
    """Extract deprecated inline blocks from aws_s3_bucket into separate resources."""
    if "aws_s3_bucket" not in code:
        return code

    bucket_resources = re.findall(r'resource\s+"aws_s3_bucket"\s+"(\w+)"', code)
    if not bucket_resources:
        return code

    extra_resources = []

    for bucket_name in bucket_resources:
        bucket_ref = f"aws_s3_bucket.{bucket_name}.id"

        # Remove acl = "private"
        acl_pattern = rf'(resource\s+"aws_s3_bucket"\s+"{bucket_name}"\s*\{{[^}}]*?)\n\s*acl\s*=\s*"[^"]*"\n'
        if re.search(acl_pattern, code, re.DOTALL):
            logger.info("[Generate] Post-fix: removing deprecated acl from aws_s3_bucket.%s", bucket_name)
            code = re.sub(acl_pattern, r'\1\n', code, count=1, flags=re.DOTALL)

        # Extract inline versioning
        vers_pattern = rf'(resource\s+"aws_s3_bucket"\s+"{bucket_name}"\s*\{{[^}}]*?)\n\s*versioning\s*\{{[^}}]*enabled\s*=\s*(true|false)[^}}]*\}}\n'
        vers_m = re.search(vers_pattern, code, re.DOTALL)
        if vers_m and 'aws_s3_bucket_versioning' not in code:
            enabled = vers_m.group(2)
            status = "Enabled" if enabled == "true" else "Suspended"
            logger.info("[Generate] Post-fix: extracting inline versioning from aws_s3_bucket.%s", bucket_name)
            code = re.sub(
                rf'\n\s*versioning\s*\{{[^}}]*enabled\s*=\s*(?:true|false)[^}}]*\}}\n',
                '\n', code, count=1, flags=re.DOTALL
            )
            extra_resources.append(
                f'\nresource "aws_s3_bucket_versioning" "{bucket_name}" {{\n'
                f'  bucket = {bucket_ref}\n'
                f'  versioning_configuration {{\n'
                f'    status = "{status}"\n'
                f'  }}\n'
                f'}}\n'
            )

        # Extract inline SSE
        sse_pattern = r'\n\s*server_side_encryption_configuration\s*\{'
        if re.search(sse_pattern, code) and 'aws_s3_bucket_server_side_encryption_configuration' not in code:
            sse_alg_m = re.search(r'sse_algorithm\s*=\s*"([^"]*)"', code)
            kms_m = re.search(r'kms_master_key_id\s*=\s*([^\n]+)', code)
            if sse_alg_m:
                logger.info("[Generate] Post-fix: extracting inline SSE from aws_s3_bucket.%s", bucket_name)
                code = re.sub(
                    r'\n\s*server_side_encryption_configuration\s*\{[^}]*\{[^}]*\{[^}]*\}[^}]*\}[^}]*\}\n',
                    '\n', code, count=1, flags=re.DOTALL
                )
                kms_line = ""
                if kms_m:
                    kms_line = f"        kms_master_key_id = {kms_m.group(1).strip()}\n"
                extra_resources.append(
                    f'\nresource "aws_s3_bucket_server_side_encryption_configuration" "{bucket_name}" {{\n'
                    f'  bucket = {bucket_ref}\n'
                    f'  rule {{\n'
                    f'    apply_server_side_encryption_by_default {{\n'
                    f'      sse_algorithm     = "{sse_alg_m.group(1)}"\n'
                    f'{kms_line}'
                    f'    }}\n'
                    f'  }}\n'
                    f'}}\n'
                )

        # Extract inline public_access_block
        pab_pattern = r'\n\s*public_access_block_configuration\s*\{([^}]*)\}\n'
        pab_m = re.search(pab_pattern, code, re.DOTALL)
        if pab_m and 'aws_s3_bucket_public_access_block' not in code:
            logger.info("[Generate] Post-fix: extracting inline public_access_block from aws_s3_bucket.%s", bucket_name)
            pab_body = pab_m.group(1)
            code = re.sub(pab_pattern, '\n', code, count=1, flags=re.DOTALL)
            extra_resources.append(
                f'\nresource "aws_s3_bucket_public_access_block" "{bucket_name}" {{\n'
                f'  bucket = {bucket_ref}\n'
                f'{pab_body}'
                f'}}\n'
            )

    if extra_resources:
        code += "\n".join(extra_resources)

    return code


def _fix_lambda_local_files(code):
    """Replace filebase64sha256 references with archive_file data source."""
    m = re.search(r'filebase64sha256\("([^"]+\.zip)"\)', code)
    if not m:
        return code

    zip_name = m.group(1)
    logger.info("[Generate] Post-fix: replacing filebase64sha256('%s') with archive_file", zip_name)

    runtime_m = re.search(r'runtime\s*=\s*"([^"]*)"', code)
    rt = runtime_m.group(1) if runtime_m else "nodejs18.x"
    if "python" in rt:
        handler_file, placeholder = "lambda_function.py", "def handler(event, context):\\n    return {'statusCode': 200, 'body': 'hello'}"
    else:
        handler_file, placeholder = "index.js", "exports.handler = async (event) => ({ statusCode: 200, body: 'hello' });"

    archive_block = (
        '\ndata "archive_file" "lambda_zip" {\n'
        '  type        = "zip"\n'
        f'  output_path = "{zip_name}"\n'
        "  source {\n"
        f'    content  = "{placeholder}"\n'
        f'    filename = "{handler_file}"\n'
        "  }\n"
        "}\n"
    )

    code = re.sub(
        r'filebase64sha256\("[^"]+\.zip"\)',
        "data.archive_file.lambda_zip.output_base64sha256", code,
    )
    code = re.sub(
        r'(filename\s*=\s*)"[^"]+\.zip"',
        r"\1data.archive_file.lambda_zip.output_path", code,
    )
    code = re.sub(
        r'(resource\s+"aws_lambda_function")',
        archive_block + r"\1", code, count=1,
    )
    return code


def _fix_apigw_stage_name(code):
    """Extract deprecated stage_name from aws_api_gateway_deployment."""
    if "aws_api_gateway_deployment" not in code or "aws_api_gateway_stage" in code:
        return code

    pattern = r'(resource\s+"aws_api_gateway_deployment"\s+"(\w+)"\s*\{[^}]*?)\n\s*stage_name\s*=\s*"([^"]+)"\n'
    m = re.search(pattern, code, re.DOTALL)
    if not m:
        return code

    deploy_name = m.group(2)
    stage_value = m.group(3)
    logger.info("[Generate] Post-fix: moving stage_name from deployment to separate stage resource")

    code = re.sub(r'\n\s*stage_name\s*=\s*"[^"]+"\n', '\n', code, count=1)

    rest_api_match = re.search(
        r'resource\s+"aws_api_gateway_deployment"\s+"' + deploy_name + r'"[^}]*rest_api_id\s*=\s*([^\n]+)',
        code, re.DOTALL,
    )
    rest_api_ref = rest_api_match.group(1).strip() if rest_api_match else "aws_api_gateway_rest_api.example.id"

    stage_block = (
        f'\nresource "aws_api_gateway_stage" "{stage_value}" {{\n'
        f"  deployment_id = aws_api_gateway_deployment.{deploy_name}.id\n"
        f"  rest_api_id   = {rest_api_ref}\n"
        f'  stage_name    = "{stage_value}"\n'
        "}\n"
    )
    code += stage_block
    return code


def _fix_templatefile_refs(code):
    """Replace templatefile() calls with inline heredoc equivalents."""
    pattern = r'templatefile\(\s*"[^"]+"\s*,\s*\{([^}]*)\}\s*\)'
    m = re.search(pattern, code, re.DOTALL)
    if not m:
        return code

    logger.info("[Generate] Post-fix: replacing templatefile() with inline heredoc")
    code = re.sub(
        pattern,
        '<<-EOF\n#!/bin/bash\necho "placeholder user data"\nEOF',
        code, count=1, flags=re.DOTALL,
    )
    return code


def apply_all_postprocessors(code):
    """Run the full chain of post-processors on generated code."""
    code = _fix_module_blocks(code)
    code = _fix_undeclared_variables(code)
    code = _fix_missing_defaults(code)
    code = _fix_s3_inline_blocks(code)
    code = _fix_s3_unique_names(code)
    code = _strip_name_suffixes(code)
    code = _fix_lambda_local_files(code)
    code = _fix_apigw_stage_name(code)
    code = _fix_templatefile_refs(code)
    code = _fix_ec2_hardcoded_ami(code)
    code = _fix_ec2_key_pair(code)
    code = _fix_eip_vpc_deprecated(code)
    code = _fix_launch_template_sg(code)
    code = _fix_placeholder_sg(code)
    code = _fix_kms_and_ebs(code)
    code = _fix_asg_launch_template(code)
    code = _fix_cloudwatch_asg_dimensions(code)
    code = _fix_asg_target_group_attachment(code)
    code = _fix_duplicate_routes(code)
    code = _fix_lb_listener_forward(code)
    code = _fix_placeholder_resources(code)
    code = _fix_unnecessary_iam_roles(code)
    return code
