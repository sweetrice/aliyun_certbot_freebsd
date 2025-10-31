#!/etc/certbot/venv/bin/python
# -*- coding: utf-8 -*-
from configparser import ConfigParser
from Tea.core import TeaCore
from alibabacloud_cas20200407.client import Client as cas20200407Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_cas20200407 import models as cas_20200407_models
from alibabacloud_tea_util.client import Client as UtilClient
from alibabacloud_tea_util import models as util_models
from alibabacloud_cdn20180510.client import Client as Cdn20180510Client
from alibabacloud_cdn20180510 import models as cdn_20180510_models
import time
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 配置项

cfg = ConfigParser()
with open('config.ini', 'r', encoding='utf-8') as cfgFileStream:
    cfg.read_string("[main]\n" + cfgFileStream.read())

LIVE_BASE = cfg['main']['live_base']
CERT_PREFIX_TEMPLATE = "a_{domain}_"
RECENT_KEEP = 1        # 每个域名保留最近多少个 CAS 证书
RETRY_COUNT = 3
RETRY_DELAY = 2        # seconds
LOG = print            # 替换为 logger.info / logger.error 如需

def getAliyunBaseConfig():
    accessKey = cfg['main']['dns_aliyun_access_key']
    accessKeySecret = cfg['main']['dns_aliyun_access_key_secret']
    aliyunConfig = open_api_models.Config(
            access_key_id=accessKey,
            access_key_secret=accessKeySecret
    )
    return aliyunConfig

def getAliyunCASClient():
    aliyunConfig = getAliyunBaseConfig()
    aliyunConfig.endpoint = f'cas.aliyuncs.com' # cn-hangzhou
    aliyunCASClient = cas20200407Client(aliyunConfig)
    return aliyunCASClient
 
def getAliyunCDNClient():
    aliyunConfig = getAliyunBaseConfig()
    aliyunConfig.endpoint = f'cdn.aliyuncs.com' # cn-hangzhou
    aliyunCDNClient = Cdn20180510Client(aliyunConfig)
    return aliyunCDNClient

def read_cert_file(path):
    with open(path, 'r') as f:
        return f.read()

def read_cert_file_bytes(path):
    return Path(path).read_bytes()

def load_cert(data: bytes):
    try:
        return x509.load_pem_x509_certificate(data, default_backend())
    except ValueError:
        return x509.load_der_x509_certificate(data, default_backend())

def issued_within_day(cert_bytes: bytes, days: float = 1.0) -> bool:
    cert = load_cert(cert_bytes)
    issued = cert.not_valid_before_utc  # 带时区的 datetime
    now = datetime.now(timezone.utc)
    return (now - issued) <= timedelta(days=days)

def upload_and_bind_multi(domains, cdn_domain_map=None):
    results = {}
    cas_client = getAliyunCASClient()
    cdn_client = getAliyunCDNClient()
    needs_action = {}
    for domain in domains:
        res_entry = {'cert_name': None, 'cdn_bind_result': None,'clean_result':None}
        try:
            fullchain = read_cert_file(f"{LIVE_BASE}/{domain}/fullchain.pem")
            privkey = read_cert_file(f"{LIVE_BASE}/{domain}/privkey.pem")
            try:
                fullchain_byte = read_cert_file_bytes(f"{LIVE_BASE}/{domain}/fullchain.pem")
                if issued_within_day(fullchain_byte, days=1.0):
                    needs_action[domain] = True
                else:
                    continue
            except Exception:
                needs_action[domain] = False
                continue

        except Exception as e:
            LOG(f"[{domain}] read cert/key failed: {e}")
            results[domain] = res_entry
            continue

        # 上传到 CAS
        timestr = datetime.now().strftime("%Y%m%d%H%M%S")
        prefix = CERT_PREFIX_TEMPLATE.format(domain=domain.replace('.', '_'))
        cert_name = f"{prefix}{timestr}"

        # 构造上传请求 —— 根据 SDK 调整类名/字段
        upload_req = cas_20200407_models.UploadUserCertificateRequest()
        upload_req.name = cert_name
        upload_req.cert = fullchain
        upload_req.key = privkey

        upload_resp = None
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                upload_resp = cas_client.upload_user_certificate(upload_req)
                break
            except Exception as e:
                LOG(f"[{domain}] upload attempt {attempt} failed: {e}")
                if attempt < RETRY_COUNT:
                    time.sleep(RETRY_DELAY)
                else:
                    LOG(f"[{domain}] upload failed after {RETRY_COUNT} attempts")
                    upload_resp = None

        if upload_resp is None:
            results[domain] = res_entry
            continue

        LOG(f"[{domain}] uploaded cert as: {cert_name}")
        res_entry['cert_name'] = cert_name
        try:
            LOG(UtilClient.to_jsonstring(TeaCore.to_map(upload_resp.body)))
        except Exception:
            LOG(upload_resp)

        # 绑定到 CDN：确定要绑定的 CDN 域名
        bind_domain = None
        if cdn_domain_map and domain in cdn_domain_map:
            bind_domain = cdn_domain_map[domain]
        else:
            bind_domain = domain

        # 使用 CDN API 绑定证书：SetDomainServerCertificate（不同 SDK 名称可能不同）
        try:
            request = cdn_20180510_models.BatchSetCdnDomainServerCertificateRequest()
            request.domain_name = bind_domain
            request.cert_name = cert_name
            request.cert_type = 'cas'
            request.sslprotocol = 'on'

            runtime = util_models.RuntimeOptions()
            bind_resp = cdn_client.batch_set_cdn_domain_server_certificate_with_options(request, runtime)
            print('update cdn https for %s' % bind_domain)
            res_entry['cdn_bind_result'] = bind_resp
            LOG(f"[{domain}] bind to CDN domain {bind_domain} succeeded")
            try:
                LOG(UtilClient.to_jsonstring(TeaCore.to_map(bind_resp.body)))
            except Exception:
                LOG(bind_resp)
        except Exception as e:
            LOG(f"[{domain}] bind to CDN domain {bind_domain} failed: {e}")

        results[domain] = res_entry

        
    # 清理旧证书（保留 RECENT_KEEP 个）
    try:
        list_req = cas_20200407_models.ListUserCertificateOrderRequest()
        list_req.order_type = 'UPLOAD'
        runtime = util_models.RuntimeOptions()
        list_resp = cas_client.list_user_certificate_order_with_options(list_req,runtime)
        items_map = TeaCore.to_map(list_resp.body)
        items = items_map.get('CertificateOrderList', []) or []
        for domain in domains:
            if needs_action.get(domain):
                LOG(f"[{domain}] 上传了新证书，清理旧证书")
                prefix = CERT_PREFIX_TEMPLATE.format(domain=domain.replace('.', '_'))
                matched = []
                for it in items:
                    name = it.get('Name')
                    if not name.startswith(prefix):
                        continue
                    matched.append({'name': name, 'certificate_id':it.get('CertificateId')})

                if not matched:
                    LOG(f"[{domain}] no old certificates found for prefix {prefix}")
                    continue

                matched.sort(key=lambda x: x.get('certificate_id'), reverse=True)
                to_delete = matched[RECENT_KEEP:]
                for entry in to_delete:
                    cert_id = entry['certificate_id']
                    try:
                        del_req = cas_20200407_models.DeleteUserCertificateRequest()
                        del_req.cert_id = cert_id
                        runtime = util_models.RuntimeOptions()
                        del_resp = cas_client.delete_user_certificate_with_options(del_req,runtime)
                        LOG(f"[{domain}] deleted old cert: {cert_id}")
                        try:
                            LOG(UtilClient.to_jsonstring(TeaCore.to_map(del_resp.body)))
                        except Exception:
                            LOG(del_resp)
                    except Exception as e:
                        LOG(f"[{domain}] failed to delete {cert_id}: {e}")
                        # 不中断其他删除
            else:              
                LOG(f"[{domain}] 未上传新证书，跳过")
                continue

    except Exception as e:
        LOG(f"[{domain}] list certificates failed: {e}")

    return results

if __name__ == '__main__':
    domains = ["domain1.com", "domain2.com"]
    cdn_map = {"domain1.com": "cdn1.domain1.com,cdn2.domain1.com", "domain2.com": "cdn1.domain2.com,cdn2.domain2.com"}
    upload_and_bind_multi(domains, cdn_map)
