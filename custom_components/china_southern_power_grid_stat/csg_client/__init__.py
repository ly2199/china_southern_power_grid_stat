# -*- coding: utf-8 -*-
"""
Implementations of CSG's Web API
this library is synchronous - since the updates are not frequent (12h+)
and each update only contains a few requests
"""
from __future__ import annotations

import datetime
import json
import logging
import random
import time
from base64 import b64decode, b64encode
from copy import copy
from hashlib import md5
from typing import Any

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from requests.adapters import HTTPAdapter

from .const import *

_LOGGER = logging.getLogger(__name__)


class CSGAPIError(Exception):
    """Generic API errors"""

    def __init__(self, sta: str, msg: str | None = None) -> None:
        """sta: status code, msg: message"""
        Exception.__init__(self)
        self.sta = sta
        self.msg = msg

    def __str__(self):
        return f"<CSGAPIError sta={self.sta} message={self.msg}>"


class CSGHTTPError(CSGAPIError):
    """Unexpected HTTP status code (!=200)"""

    def __init__(self, code: int) -> None:
        CSGAPIError.__init__(self, sta=f"HTTP{code}")
        self.status_code = code

    def __str__(self) -> str:
        return f"<CSGHTTPError code={self.status_code}>"


class InvalidCredentials(CSGAPIError):
    """Wrong username+password combination (RESP_STA_LOGIN_WRONG_CREDENTIAL)"""

    def __str__(self):
        return f"<CSGInvalidCredentials sta={self.sta} message={self.msg}>"


class NotLoggedIn(CSGAPIError):
    """Not logged in or login expired (RESP_STA_NO_LOGIN)"""

    def __str__(self):
        return f"<CSGNotLoggedIn sta={self.sta} message={self.msg}>"


class QrCodeExpired(Exception):
    """QR code has expired"""


def generate_qr_login_id():
    """
    Generate a unique id for qr code login
    word-by-word copied from js code
    """
    rand_str = f"{int(time.time() * 1000)}{random.random()}"
    return md5(rand_str.encode()).hexdigest()


def encrypt_credential(password: str) -> str:
    """Use RSA+pubkey to encrypt password"""
    rsa_key = RSA.import_key(b64decode(CREDENTIAL_PUBKEY))
    credential_cipher = PKCS1_v1_5.new(rsa_key)
    encrypted_pwd = credential_cipher.encrypt(password.encode("utf8"))
    return b64encode(encrypted_pwd).decode()


def encrypt_params(params: dict) -> str:
    """Decrypt response message using AES with KEY, IV"""
    json_cipher = AES.new(PARAM_KEY, AES.MODE_CBC, PARAM_IV)

    def pad(content: str) -> str:
        return content + (16 - len(content) % 16) * "\x00"

    json_str = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    encrypted = json_cipher.encrypt(pad(json_str).encode("utf8"))
    return b64encode(encrypted).decode()


def decrypt_params(encrypted: str) -> dict:
    """Encrypt request message using AES with KEY, IV"""
    json_cipher = AES.new(PARAM_KEY, AES.MODE_CBC, PARAM_IV)
    decrypted = json_cipher.decrypt(b64decode(encrypted))
    # remove padding
    params = json.loads(decrypted.decode().strip("\x00"))
    return params


class CSGElectricityAccount:
    """Represents one electricity account, identified by account number (缴费号)"""

    def __init__(
        self,
        account_number: str | None = None,
        area_code: str | None = None,
        ele_customer_id: str | None = None,
        metering_point_id: str | None = None,
        metering_point_number: str | None = None,
        address: str | None = None,
        user_name: str | None = None,
    ) -> None:
        # the parameters are independent for each electricity account

        # the 16-digit billing number, as a unique identifier, not used in api for now
        self.account_number = account_number

        self.area_code = area_code

        # this may change on every login, alternative name in js code is `binding_id`
        self.ele_customer_id = ele_customer_id

        # in fact one account may have multiple metering points,
        # however for individual users there should only be one
        self.metering_point_id = metering_point_id
        self.metering_point_number = metering_point_number

        # for frontend display only
        self.address = address
        self.user_name = user_name

    def dump(self) -> dict[str, str]:
        """serialize this object"""
        return {
            ATTR_ACCOUNT_NUMBER: self.account_number,
            ATTR_AREA_CODE: self.area_code,
            ATTR_ELE_CUSTOMER_ID: self.ele_customer_id,
            ATTR_METERING_POINT_ID: self.metering_point_id,
            ATTR_METERING_POINT_NUMBER: self.metering_point_number,
            ATTR_ADDRESS: self.address,
            ATTR_USER_NAME: self.user_name,
        }

    @staticmethod
    def load(data: dict) -> CSGElectricityAccount:
        """deserialize this object"""
        for k in (
            ATTR_ACCOUNT_NUMBER,
            ATTR_AREA_CODE,
            ATTR_ELE_CUSTOMER_ID,
            ATTR_METERING_POINT_ID,
            ATTR_ADDRESS,
            ATTR_USER_NAME,
        ):
            if k not in data:
                raise ValueError(f"Missing key {k}")
        # ATTR_METERING_POINT_NUMBER is added in later version, skip check here
        # TODO: add ATTR_METERING_POINT_NUMBER to the check in the future
        account = CSGElectricityAccount(
            account_number=data[ATTR_ACCOUNT_NUMBER],
            area_code=data[ATTR_AREA_CODE],
            ele_customer_id=data[ATTR_ELE_CUSTOMER_ID],
            metering_point_id=data[ATTR_METERING_POINT_ID],
            metering_point_number=data.get(ATTR_METERING_POINT_NUMBER),
            address=data[ATTR_ADDRESS],
            user_name=data[ATTR_USER_NAME],
        )
        return account


class CSGClient:
    """
    Implementation of APIs from CSG iOS app interface.
    Parameters and consts are from web app js, however, these interfaces are virtually the same

    Do not call any functions starts with _api unless you are certain about what you're doing

    How to use:
    First call one of the functions to login (see example code)
    Then call `CSGClient.initialize` *important
    To get all linked electricity accounts, call `get_all_electricity_accounts`
    Use the account objects to call the utility functions and wrapped api functions
    """

    def __init__(
        self,
        auth_token: str | None = None,
    ) -> None:
        self._session: requests.Session = requests.Session()
        # the coordinator fetches many endpoints concurrently on this one
        # session, enlarge the pool to avoid discarding connections
        self._session.mount(
            "https://",
            HTTPAdapter(pool_connections=10, pool_maxsize=30),
        )
        self._session.mount(
            "http://",
            HTTPAdapter(pool_connections=10, pool_maxsize=30),
        )
        self._common_headers = {
            "Host": "95598.csg.cn",
            "Content-Type": "application/json;charset=utf-8",
            "Origin": "file://",
            HEADER_X_AUTH_TOKEN: "",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko)",
            HEADER_CUST_NUMBER: "",
            "Accept-Language": "zh-CN,cn;q=0.9",
        }

        self.auth_token = auth_token

        # identifier, need to be set in initialize()
        self.customer_number = None
        # user info from getUserInfo, cached by initialize()
        self.user_info: dict[str, Any] | None = None

    # begin internal utility functions
    def _make_request(
        self,
        path: str,
        payload: dict | None,
        with_auth: bool = True,
        method: str = "POST",
        custom_headers: dict | None = None,
        base_path: str = BASE_PATH_APP,
    ):
        """
        Function to make the http request to api endpoints
        can automatically add authentication header(s)
        """
        _LOGGER.debug(
            "_make_request: %s, data=%s, auth=%s, method=%s",
            path,
            payload,
            with_auth,
            method,
        )
        url = base_path + path
        headers = copy(self._common_headers)
        if custom_headers:
            for _k, _v in custom_headers.items():
                headers[_k] = _v
        if with_auth:
            headers[HEADER_X_AUTH_TOKEN] = self.auth_token
            headers[HEADER_CUST_NUMBER] = self.customer_number
        if method == "POST":
            response = self._session.post(url, json=payload, headers=headers, timeout=60)
            if response.status_code != 200:
                _LOGGER.error(
                    "API call %s returned status code %d", path, response.status_code
                )
                raise CSGHTTPError(response.status_code)

            json_str = response.content.decode("utf-8", errors="ignore").strip()
            try:
                json_data = json.loads(json_str)
            except json.JSONDecodeError:
                # tolerate a response body that wraps the json object with extra text
                start, end = json_str.find("{"), json_str.rfind("}")
                if start == -1 or end == -1:
                    raise CSGAPIError(
                        "HTTP200", f"invalid response body: {json_str[:200]}"
                    ) from None
                json_data = json.loads(json_str[start : end + 1])
            response_data = json_data
            _LOGGER.debug(
                "_make_request: %s, response: %s",
                path,
                json.dumps(response_data, ensure_ascii=False),
            )

            # headers need to be returned since they may contain additional data
            return response.headers, response_data

        raise NotImplementedError()

    def _handle_unsuccessful_response(self, api_path: str, response_data: dict):
        """Handles sta=!RESP_STA_SUCCESS"""
        _LOGGER.debug(
            "Account customer number: %s, unsuccessful response while calling %s: %s",
            self.customer_number,
            api_path,
            response_data,
        )

        if response_data[JSON_KEY_STA] == RESP_STA_NO_LOGIN:
            raise NotLoggedIn(
                response_data[JSON_KEY_STA], response_data.get(JSON_KEY_MESSAGE)
            )
        raise CSGAPIError(
            response_data[JSON_KEY_STA], response_data.get(JSON_KEY_MESSAGE)
        )

    # end internal utility functions

    # begin raw api functions
    def api_send_login_sms(self, phone_no: str):
        """Send SMS verification code to phone_no
        Note this is not the function for login with SMS, it only requests to send the code
        """
        path = "center/sendMsg"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            "phoneNumber": phone_no,
            "vcType": VERIFICATION_CODE_TYPE_LOGIN,
            "msgType": SEND_MSG_TYPE_VERIFICATION_CODE,
        }
        _, resp_data = self._make_request(path, payload, with_auth=False)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return True
        self._handle_unsuccessful_response(path, resp_data)

    def api_create_login_qr_code(
        self, channel: QRCodeType, login_id: str | None = None
    ) -> (str, str):
        """Request API to create a QR code for login
        Returns login_id and link to QR code image
        """
        path = "center/createLoginQrcode"

        login_id = login_id or generate_qr_login_id()
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            "channel": channel,
            # NOTE: this spell error is intentional
            "lgoinId": login_id,
        }
        _, resp_data = self._make_request(
            path, payload, with_auth=False, base_path=BASE_PATH_WEB
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return login_id, resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_get_qr_login_status(self, login_id: str) -> (bool, str):
        """Get login status of the QR code"""
        path = "center/getLoginInfo"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            # this one is the correct spelling
            "loginId": login_id,
        }
        resp_header, resp_data = self._make_request(
            path, payload, with_auth=False, base_path=BASE_PATH_WEB
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return True, resp_header[HEADER_X_AUTH_TOKEN]
        if resp_data[JSON_KEY_STA] == RESP_STA_QR_NOT_SCANNED:
            return False, ""
        self._handle_unsuccessful_response(path, resp_data)

    def api_login_with_sms_code(self, phone_no: str, sms_code: str):
        """Login with phone number and SMS code"""
        path = "center/login"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            JSON_KEY_ACCT_ID: phone_no,
            JSON_KEY_LOGON_CHAN: LOGON_CHANNEL_HANDHELD_HALL,
            JSON_KEY_CRED_TYPE: LOGIN_TYPE_PHONE_CODE,
            JSON_KEY_SMS_CODE: sms_code,
        }
        payload = {JSON_KEY_PARAM: encrypt_params(payload)}
        resp_header, resp_data = self._make_request(
            path, payload, with_auth=False, custom_headers={"need-crypto": "true"}
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_header[HEADER_X_AUTH_TOKEN]
        self._handle_unsuccessful_response(path, resp_data)

    def api_login_with_password_and_sms_code(
        self, phone_no: str, password: str, sms_code: str
    ):
        """Login with phone number, SMS code and password"""
        path = "center/loginByPwdAndMsg"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            JSON_KEY_ACCT_ID: phone_no,
            JSON_KEY_LOGON_CHAN: LOGON_CHANNEL_HANDHELD_HALL,
            JSON_KEY_CRED_TYPE: LOGIN_TYPE_PHONE_PWD_CODE,
            "credentials": encrypt_credential(password),
            JSON_KEY_SMS_CODE: sms_code,
            "checkPwd": True,
        }
        payload = {JSON_KEY_PARAM: encrypt_params(payload)}
        resp_header, resp_data = self._make_request(
            path, payload, with_auth=False, custom_headers={"need-crypto": "true"}
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_header[HEADER_X_AUTH_TOKEN]
        if resp_data[JSON_KEY_STA] == RESP_STA_LOGIN_WRONG_CREDENTIAL:
            raise InvalidCredentials(
                resp_data[JSON_KEY_STA], resp_data.get(JSON_KEY_MESSAGE)
            )
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_authentication_result(self) -> dict[str, Any]:
        """Contains custNumber, used to verify login"""
        path = "user/queryAuthenticationResult"
        payload = None
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_get_user_info(self) -> dict[str, Any]:
        """Get account info"""
        path = "user/getUserInfo"
        payload = None
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_get_all_linked_electricity_accounts(self) -> list[dict[str, Any]]:
        """List all linked electricity accounts under this account"""
        path = "eleCustNumber/queryBindEleUsers"
        _, resp_data = self._make_request(path, {})
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            _LOGGER.debug(
                "Total %d users under this account", len(resp_data[JSON_KEY_DATA])
            )
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_get_metering_point(
        self,
        area_code: str,
        ele_customer_id: str,
    ) -> dict:
        """Get metering point id"""
        path = "charge/queryMeteringPoint"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            "eleCustNumberList": [
                {JSON_KEY_ELE_CUST_ID: ele_customer_id, JSON_KEY_AREA_CODE: area_code}
            ],
        }
        # custom_headers = {"funid": "100t002"}
        custom_headers = {}
        _, resp_data = self._make_request(path, payload, custom_headers=custom_headers)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_day_electric_by_m_point(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
    ) -> dict:
        """get usage(kWh) by day in the given month"""
        path = "charge/queryDayElectricByMPoint"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_YEAR_MONTH: f"{year}{month:02d}",
            JSON_KEY_METERING_POINT_ID: metering_point_id,
        }
        # custom_headers = {"funid": "100t002"}
        custom_headers = {}
        _, resp_data = self._make_request(path, payload, custom_headers=custom_headers)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_day_electric_charge_by_m_point(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
    ) -> dict:
        """get charge by day in the given month
        KNOWN BUG: this api call returns the daily cost data of year_month,
        but the ladder data will be this month's.
        this api call could take a long time to return (~30s)
        """
        path = "charge/queryDayElectricChargeByMPoint"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_YEAR_MONTH: f"{year}{month:02d}",
            JSON_KEY_METERING_POINT_ID: metering_point_id,
        }
        # custom_headers = {"funid": "100t002"}  # TODO: what does this do? region?
        custom_headers = {}
        _, resp_data = self._make_request(path, payload, custom_headers=custom_headers)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_day_electric_and_temperature(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
    ) -> dict:
        """get power in kWh, hi/lo temperature by day in the given month"""
        path = "charge/queryDayElectricAndTemperature"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_YEAR_MONTH: f"{year}{month:02d}",
            JSON_KEY_METERING_POINT_ID: metering_point_id,
        }
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_electricity_calender(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
        metering_point_number: str,
    ) -> dict:
        """get power in kWh, hi/lo/avg temperature by day in the given month"""
        path = "charge/queryElectricityCalendar"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_YEAR_MONTH: f"{year}{month:02d}",
            JSON_KEY_METERING_POINT_ID: metering_point_id,
            "deviceIdentif": metering_point_number,
        }
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_account_surplus(self, area_code: str, ele_customer_id: str):
        """Contains: balance and arrears"""
        path = "charge/queryUserAccountNumberSurplus"
        payload = {JSON_KEY_AREA_CODE: area_code, JSON_KEY_ELE_CUST_ID: ele_customer_id}
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_get_fee_analyze_details(
        self, year: int, area_code: str, ele_customer_id: str
    ):
        """
        Contains: year total kWh, year total charge, kWh/charge by month in current year
        """
        path = "charge/getAnalyzeFeeDetails"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            "electricityBillYear": year,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_METERING_POINT_ID: None,  # this is set to null in api
        }
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_day_electric_by_m_point_yesterday(
        self,
        area_code: str,
        ele_customer_id: str,
    ) -> dict:
        """Contains: power consumption(kWh) of yesterday"""
        path = "charge/queryDayElectricByMPointYesterday"
        payload = {JSON_KEY_ELE_CUST_ID: ele_customer_id, JSON_KEY_AREA_CODE: area_code}
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_query_charges(self, area_code: str, ele_customer_id: str, _type="0"):
        """Contains: balance and arrears, metering points"""
        path = "charge/queryCharges"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            "eleModels": [
                {JSON_KEY_ELE_CUST_ID: ele_customer_id, JSON_KEY_AREA_CODE: area_code}
            ],
            "type": _type,
        }
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_select_elec_bill_details(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        cur_meter_reading_times: int = 1,
    ) -> dict:
        """Get monthly electricity bill details (total charge/kwh, ladder info)
        This endpoint is used as a fallback for regions where the daily charge
        api (`queryDayElectricChargeByMPoint`) is not supported, e.g. Yunnan.
        """
        path = "charge/selectElecBillDetails"
        payload = {
            "electricityBillYearMonth": f"{year}{month:02d}",
            "curMeterReadingTimes": cur_meter_reading_times,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_AREA_CODE: area_code,
        }
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_select_bill_charts_list(
        self,
        start_year: int,
        start_month: int,
        end_year: int,
        end_month: int,
        area_code: str,
        ele_customer_id: str,
    ) -> list[dict]:
        """Get a list of monthly bills in the given year-month range"""
        path = "charge/selectBillchartsList"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            "startYearMonth": f"{start_year}{start_month:02d}",
            "endYearMonth": f"{end_year}{end_month:02d}",
        }
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def api_logout(self, logon_chan: str, cred_type: LoginType) -> None:
        """logout"""
        path = "center/logout"
        payload = {JSON_KEY_LOGON_CHAN: logon_chan, JSON_KEY_CRED_TYPE: cred_type}
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    # end raw api functions

    # begin utility functions
    @staticmethod
    def load(data: dict[str, str]) -> CSGClient:
        """
        Restore the session info to client object
        The validity of the session won't be checked
        `initialize()` needs to be called for the client to be usable
        """
        for k in (ATTR_AUTH_TOKEN,):
            if not data.get(k):
                raise ValueError(f"missing parameter: {k}")
        client = CSGClient(
            auth_token=data[ATTR_AUTH_TOKEN],
        )
        return client

    def dump(self) -> dict[str, Any]:
        """Dump the session to dict"""
        return {
            ATTR_AUTH_TOKEN: self.auth_token,
        }

    def set_authentication_params(self, auth_token: str):
        """Set self.auth_token and client generated cookies"""
        self.auth_token = auth_token

    def initialize(self):
        """Initialize the client"""
        resp_data = self.api_get_user_info()
        self.customer_number = resp_data[JSON_KEY_CUST_NUMBER]
        self.user_info = resp_data

    def verify_login(self) -> bool:
        """Verify validity of the session"""
        try:
            self.api_query_authentication_result()
        except CSGAPIError as err:
            if isinstance(err, CSGHTTPError):
                # network/server error, the session state is unknown
                raise
            return False
        return True

    def logout(self, login_type: LoginType):
        """Logout and reset identifier, token etc."""
        self.api_logout(LOGON_CHANNEL_HANDHELD_HALL, login_type)
        self.auth_token = None
        self.customer_number = None

    # end utility functions

    # begin high-level api wrappers

    def get_all_electricity_accounts(self) -> list[CSGElectricityAccount]:
        """Get all electricity accounts linked to current account"""
        result = []
        ele_user_resp_data = self.api_get_all_linked_electricity_accounts()

        for item in ele_user_resp_data:
            metering_point_data = self.api_get_metering_point(
                item[JSON_KEY_AREA_CODE], item["bindingId"]
            )
            metering_point_id = metering_point_data[0][JSON_KEY_METERING_POINT_ID]
            metering_point_number = metering_point_data[0][
                JSON_KEY_METERING_POINT_NUMBER
            ]
            account = CSGElectricityAccount(
                account_number=item["eleCustNumber"],
                area_code=item[JSON_KEY_AREA_CODE],
                ele_customer_id=item["bindingId"],
                metering_point_id=metering_point_id,
                metering_point_number=metering_point_number,
                address=item["eleAddress"],
                user_name=item["userName"],
            )
            result.append(account)
        return result

    def get_month_daily_usage_detail(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> tuple[float, list[dict[str, str | float]]]:
        """Get daily usage of current month"""

        year, month = year_month

        resp_data = self.api_query_day_electric_by_m_point(
            year,
            month,
            account.area_code,
            account.ele_customer_id,
            account.metering_point_id,
        )
        month_total_kwh = float(resp_data["totalPower"])
        by_day = []
        for d_data in resp_data["result"]:
            by_day.append(
                {WF_ATTR_DATE: d_data["date"], WF_ATTR_KWH: float(d_data["power"])}
            )
        return month_total_kwh, by_day

    def get_month_daily_cost_detail(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> tuple[float | None, float | None, dict, list[dict[str, str | float]]]:
        """Get daily cost of current month"""

        year, month = year_month

        try:
            resp_data = self.api_query_day_electric_charge_by_m_point(
                year,
                month,
                account.area_code,
                account.ele_customer_id,
                account.metering_point_id,
            )
        except NotLoggedIn:
            raise
        except CSGAPIError:
            # the daily charge api is not supported in some regions (e.g. Yunnan),
            # fall back to the monthly bill which contains total charge/kwh
            _LOGGER.debug(
                "Daily charge api unavailable for account %s %d-%02d, "
                "falling back to monthly bill details",
                account.account_number,
                year,
                month,
            )
            try:
                bill_data = self.api_select_elec_bill_details(
                    year, month, account.area_code, account.ele_customer_id
                )
            except CSGAPIError:
                return None, None, self._empty_ladder(), []
            bill_detail = bill_data["billDetail"][0]
            return (
                float(bill_data["totalElectricity"]),
                float(bill_detail["totalPower"]),
                self._empty_ladder(),
                [],
            )

        by_day = []
        for d_data in resp_data["result"]:
            by_day.append(
                {
                    WF_ATTR_DATE: d_data["date"],
                    WF_ATTR_CHARGE: float(d_data["charge"]),
                    WF_ATTR_KWH: float(d_data["power"]),
                }
            )

        # sometimes the data by day is present, but the total amount and ladder are not

        if resp_data["totalElectricity"] is not None:
            month_total_cost = float(resp_data["totalElectricity"])
        else:
            month_total_cost = None

        if resp_data["totalPower"] is not None:
            month_total_kwh = float(resp_data["totalPower"])
        else:
            month_total_kwh = None

        # sometimes the ladder info is null, handle that
        if resp_data["ladderEle"] is not None:
            current_ladder = int(resp_data["ladderEle"])
        else:
            current_ladder = None
        # "2023-05-01 00:00:00.0"
        if resp_data["ladderEleStartDate"] is not None:
            current_ladder_start_date = self._parse_ladder_start_date(
                resp_data["ladderEleStartDate"]
            )
        else:
            current_ladder_start_date = None
        if resp_data["ladderEleSurplus"] is not None:
            current_ladder_remaining_kwh = float(resp_data["ladderEleSurplus"])
        else:
            current_ladder_remaining_kwh = None
        if resp_data["ladderEleTariff"] is not None:
            current_tariff = float(resp_data["ladderEleTariff"])
        else:
            current_tariff = None
        # TODO what will happen to `current_ladder_remaining_kwh` when it's the last ladder?
        ladder = {
            WF_ATTR_LADDER: current_ladder,
            WF_ATTR_LADDER_START_DATE: current_ladder_start_date,
            WF_ATTR_LADDER_REMAINING_KWH: current_ladder_remaining_kwh,
            WF_ATTR_LADDER_TARIFF: current_tariff,
        }

        return month_total_cost, month_total_kwh, ladder, by_day

    @staticmethod
    def _empty_ladder() -> dict:
        """Ladder dict for regions without ladder info (e.g. Yunnan)"""
        return {
            WF_ATTR_LADDER: None,
            WF_ATTR_LADDER_START_DATE: None,
            WF_ATTR_LADDER_REMAINING_KWH: None,
            WF_ATTR_LADDER_TARIFF: None,
        }

    @staticmethod
    def _parse_ladder_start_date(date_str: str) -> datetime.datetime:
        """Parse the ladder start date, tolerating different server formats"""
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        raise ValueError(f"unrecognized ladder start date format: {date_str}")

    def get_balance_and_arrears(
        self, account: CSGElectricityAccount
    ) -> tuple[float, float]:
        """Get account balance and arrears"""

        resp_data = self.api_query_account_surplus(
            account.area_code, account.ele_customer_id
        )
        balance = resp_data[0]["balance"]
        arrears = resp_data[0]["arrears"]
        return float(balance), float(arrears)

    def get_year_month_stats(
        self, account: CSGElectricityAccount, year
    ) -> tuple[float, float, list[dict[str, str | float]]]:
        """Get year total kWh, year total charge, kWh/charge by month in current year"""

        resp_data = self.api_get_fee_analyze_details(
            year, account.area_code, account.ele_customer_id
        )

        total_year_kwh = resp_data["totalBillingElectricity"]
        total_year_charge = resp_data["totalActualAmount"]
        by_month = []
        for m_data in resp_data["electricAndChargeList"]:
            by_month.append(
                {
                    WF_ATTR_MONTH: m_data[JSON_KEY_YEAR_MONTH],
                    WF_ATTR_CHARGE: float(m_data["actualTotalAmount"]),
                    WF_ATTR_KWH: float(m_data["billingElectricity"]),
                }
            )
        return float(total_year_charge), float(total_year_kwh), by_month

    def get_yesterday_kwh(self, account: CSGElectricityAccount) -> float:
        """Get power consumption(kwh) of yesterday"""
        resp_data = self.api_query_day_electric_by_m_point_yesterday(
            account.area_code, account.ele_customer_id
        )
        if resp_data["power"] is not None:
            return float(resp_data["power"])

    def api_query_annual_tier_info(
        self,
        year_month: tuple[int, int],
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
    ) -> dict:
        """Get the annual electricity tier info (ladder stages and prices)"""
        path = "charge/queryAnnualElectricityTierInfo"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_METERING_POINT_ID: metering_point_id,
            JSON_KEY_YEAR_MONTH: f"{year_month[0]}{year_month[1]:02d}",
        }
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def get_annual_ladder_info(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> dict:
        """Get the annual ladder info of the account.

        Returns the raw data enriched with a human readable summary, e.g.:
        {
          "business_date", "annual_kwh", "gear_left", "current_gear",
          "current_price", "start_date", "end_date", "ladder_list", "summary"
        }
        """
        resp = self.api_query_annual_tier_info(
            year_month,
            account.area_code,
            account.ele_customer_id,
            account.metering_point_id,
        )
        ladder_list = []
        for item in resp.get("ladderInfoList") or []:
            bottom = item.get("threshholdBottom")
            top = item.get("threshholdTop")
            ladder_list.append(
                {
                    "name": item.get("ladderName"),
                    "bottom": bottom,
                    "top": top,
                    "price": item.get("priceValue"),
                }
            )

        summary_lines = [
            f"截至{resp.get('businessDate')},您的年度阶梯累计电量为"
            f"{resp.get('totalElectricityOfYear')}千瓦时（度），处于{resp.get('currentGear')}阶梯， "
            f"当前阶梯电价为{resp.get('currentElectricityPrice')}元/千瓦时， "
            f"当前阶梯剩余电量为{resp.get('gearPowerLeft')}千瓦时（度）。"
        ]
        for ladder in ladder_list:
            top = ladder["top"]
            if top and int(top) >= 9999999:
                range_str = f"{ladder['bottom']}kW.h以上"
            else:
                range_str = f"{ladder['bottom']}-{top}kW.h"
            summary_lines.append(
                f"{ladder['name']}（{range_str}）\n{ladder['price']}元/千瓦时"
            )
        summary_lines.append(
            f"您的年阶梯电量起止日期为{resp.get('startDate')}-{resp.get('endDate')}"
        )

        return {
            "business_date": resp.get("businessDate"),
            "annual_kwh": resp.get("totalElectricityOfYear"),
            "gear_left": resp.get("gearPowerLeft"),
            "current_gear": resp.get("currentGear"),
            "current_price": resp.get("currentElectricityPrice"),
            "start_date": resp.get("startDate"),
            "end_date": resp.get("endDate"),
            "ladder_list": ladder_list,
            "summary": "\n".join(summary_lines),
        }

    def get_month_daily_calendar(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> tuple[float | None, float | None, float | None, list[dict], dict]:
        """Get daily power and temperature of the month from the electricity
        calendar. Returns (avg_temp, max_temp, min_temp, by_day, summary) where
        by_day items contain date, kwh, max_temp, min_temp, avg_temp, and
        summary contains non-null month aggregates (e.g. total power, peak/valley
        periods where available).
        """
        year, month = year_month
        # prefer the day electric + temperature api (fresher data), fall back
        # to the electricity calendar api
        try:
            resp_data = self.api_query_day_electric_and_temperature(
                year,
                month,
                account.area_code,
                account.ele_customer_id,
                account.metering_point_id,
            )
            date_prefix = str(year)
        except CSGAPIError:
            resp_data = self.api_query_electricity_calender(
                year,
                month,
                account.area_code,
                account.ele_customer_id,
                account.metering_point_id,
                account.metering_point_number,
            )
            date_prefix = ""
        if isinstance(resp_data, list):
            items = resp_data
        else:
            items = resp_data.get("result") or []
        by_day = []
        for d in items:
            date_str = d["date"]
            if date_str.startswith("-"):
                date_str = date_prefix + date_str
            entry = {WF_ATTR_DATE: date_str}
            if d.get("power") is not None:
                entry[WF_ATTR_KWH] = float(d["power"])
            if d.get("maxTemperature") is not None:
                entry[WF_ATTR_MAX_TEMP] = float(d["maxTemperature"])
            if d.get("minTemperature") is not None:
                entry[WF_ATTR_MIN_TEMP] = float(d["minTemperature"])
            if d.get("averageTemperature") is not None:
                entry[WF_ATTR_AVG_TEMP] = float(d["averageTemperature"])
            by_day.append(entry)

        avgs = [e[WF_ATTR_AVG_TEMP] for e in by_day if WF_ATTR_AVG_TEMP in e]
        maxs = [e[WF_ATTR_MAX_TEMP] for e in by_day if WF_ATTR_MAX_TEMP in e]
        mins = [e[WF_ATTR_MIN_TEMP] for e in by_day if WF_ATTR_MIN_TEMP in e]
        avg_temp = sum(avgs) / len(avgs) if avgs else None
        max_temp = max(maxs) if maxs else None
        min_temp = min(mins) if mins else None

        summary: dict[str, Any] = {}
        if isinstance(resp_data, dict):
            for src_key, dst_key in (
                ("totalPower", "total_power"),
                ("totalPeakPeriod", "peak_period"),
                ("totalAcme", "acme"),
                ("totalValleyStage", "valley_stage"),
                ("totalParallelPeriod", "parallel_period"),
                ("meteringPointNumber", "metering_point_number"),
                ("deviceIdentif", "device_identif"),
            ):
                value = resp_data.get(src_key)
                if value is not None:
                    summary[dst_key] = value
        if "total_power" not in summary:
            # the day electric + temperature api has no month total, compute it
            summary["total_power"] = round(
                sum(e[WF_ATTR_KWH] for e in by_day if WF_ATTR_KWH in e), 2
            )
        return avg_temp, max_temp, min_temp, by_day, summary

    def get_bill_details(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> dict:
        """Get the raw bill detail dict of the given year-month"""
        year, month = year_month
        bill_data = self.api_select_elec_bill_details(
            year, month, account.area_code, account.ele_customer_id
        )
        return bill_data["billDetail"][0]

    def get_bill_history(
        self, account: CSGElectricityAccount
    ) -> list[dict[str, str | float]]:
        """Get the recent 12 months of bills: [{month, charge, kwh}]

        Note: the server ignores the requested year-month range and always
        returns the most recent 12 bills.
        """
        now = datetime.datetime.now()
        bills = self.api_select_bill_charts_list(
            now.year - 1, 1, now.year, 12, account.area_code, account.ele_customer_id
        )
        result = []
        for b in bills:
            result.append(
                {
                    WF_ATTR_MONTH: str(b["electricityBillYearMonth"]),
                    WF_ATTR_CHARGE: float(b["totalElectricity"]),
                    WF_ATTR_KWH: float(b["totalPower"]),
                }
            )
        result.sort(key=lambda x: x[WF_ATTR_MONTH])
        return result

    def get_account_info(self, account: CSGElectricityAccount) -> dict:
        """Get all useful account info: user, address, arrears, balance details"""
        resp_data = self.api_query_charges(account.area_code, account.ele_customer_id)
        ele = resp_data[0]["ele"]

        info: dict[str, Any] = {
            WF_ATTR_USER_NAME: ele.get("userName"),
            WF_ATTR_ADDRESS: ele.get("eleAddress"),
            WF_ATTR_ARREARS_STATUS: ele.get("arrearsStatusCode"),
            "total_owned_fee": ele.get("totalOwnedFee"),
            "user_ele_type": ele.get("userEleType"),
        }

        # balance details from the surplus api
        try:
            surplus = self.api_query_account_surplus(
                account.area_code, account.ele_customer_id
            )[0]
            meter = (surplus.get("electricMeterModel") or [{}])[0]
            info["settle_acct_number"] = surplus.get("settleAcctNumber")
            info["locked_balance"] = surplus.get("lockedBalance")
            info["meter_device_id"] = meter.get("deviceIdentif")
            info["meter_asset_id"] = meter.get("runningEnergymeterId")
        except CSGAPIError:
            pass

        # user info cached by initialize()
        if self.user_info:
            info["mobile"] = self.user_info.get("mobile")
            info["wechat_nickname"] = self.user_info.get("wechatNickname")
            info["is_identity_verified"] = self.user_info.get("isIdentityVerified")

        return {k: v for k, v in info.items() if v is not None}

    def get_payment_history(
        self, account: CSGElectricityAccount, limit: int = 10
    ) -> list[dict[str, str | float]]:
        """Get the recent payment/recharge history of the account.

        Returns [{time, way, amount, note}] ordered by time descending.
        """
        surplus = self.api_query_account_surplus(
            account.area_code, account.ele_customer_id
        )[0]
        settle_acct_id = surplus.get("settleAcctId")
        if not settle_acct_id:
            return []
        resp_data = self.api_query_measurement_point_detailed(
            account.area_code, settle_acct_id
        )
        result = []
        for item in resp_data.get("balanceHistoryList") or []:
            amount = item.get("advanceBalance")
            entry = {
                "time": item.get("actualTime"),
                "way": item.get("tradeWayCode"),
                "note": item.get("accessInstructions"),
            }
            if amount is not None:
                entry["amount"] = float(amount)
            result.append({k: v for k, v in entry.items() if v is not None})
        return result[:limit]

    def api_query_measurement_point_detailed(
        self, area_code: str, settle_acct_id: str
    ) -> dict:
        """Get balance change/payment history of the settle account"""
        path = "charge/queryMeasurementPointDetailed"
        payload = {JSON_KEY_AREA_CODE: area_code, "settleAcctId": settle_acct_id}
        _, resp_data = self._make_request(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    def get_outage_notices(
        self, account: CSGElectricityAccount
    ) -> list[dict[str, str]]:
        """Get the maintenance/outage notices of the account's region.

        Returns [{name, time}] of the recent months. Note: the per-account
        outage event query is not supported in all regions (e.g. Yunnan),
        this region-level notice list is the only outage info available there.
        """
        path = "powerFailureQuery/query/notice/all"
        payload = {JSON_KEY_AREA_CODE: account.area_code}
        _, resp_data = self._make_request(path, payload, with_auth=False)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            result = []
            for item in resp_data[JSON_KEY_DATA] or []:
                result.append(
                    {
                        "name": item.get("noticeName"),
                        "time": item.get("noticeTime"),
                    }
                )
            return result
        self._handle_unsuccessful_response(path, resp_data)

    def get_power_outages(
        self,
        account: CSGElectricityAccount,
        latitude: float,
        longitude: float,
        forecast_days: int = 7,
    ) -> dict:
        """Query planned/fault power outages near the given coordinates.

        Locates the region/supply unit by coordinates (like the CSG app's
        location-based outage query), then queries planned (state=2) and
        fault (state=3) outages around the account.

        Returns {
            "region": {province, city, county, town, supply_unit},
            "events": [{range, start_time, end_time, state, type, is_electric, event_id}],
            "ongoing": number of events that are ongoing or upcoming,
        }
        """
        # 1. locate the region and supply unit by coordinates
        path = "outageRepair/queryAdminByLocation"
        payload = {"latitude": str(latitude), "longitude": str(longitude)}
        _, resp_data = self._make_request(path, payload, with_auth=True)
        if resp_data[JSON_KEY_STA] != RESP_STA_SUCCESS:
            self._handle_unsuccessful_response(path, resp_data)
        location = resp_data[JSON_KEY_DATA]["data"]
        org = (location.get("orgList") or [{}])[0]
        bureau_code = org.get("bureauCode")
        if not bureau_code:
            raise CSGAPIError(
                RESP_STA_SYSTEM_ERROR,
                "no supply unit found for the given location",
            )
        region = {
            "province": location.get("province"),
            "city": location.get("city"),
            "county": location.get("county"),
            "town": location.get("town"),
            "supply_unit": org.get("orgName"),
        }
        region = {k: v for k, v in region.items() if v is not None}

        # 2. query planned and fault outages
        now = datetime.datetime.now()
        start = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        end = (now + datetime.timedelta(days=forecast_days)).strftime(
            "%Y-%m-%d 23:59:59"
        )
        base_query = {
            "unitCode": bureau_code,
            "supplyUnitId": org.get("mrId"),
            "provinceCode": bureau_code[:2],
            "regionCode": location.get("townCode") or location.get("countyCode"),
            "type": "2",
            "address": account.account_number,
            "outageStartDate": start,
            "outageEndDate": end,
            "pageNo": 1,
            "pageSize": 10,
        }
        events = []
        for state in ("2", "3"):
            query = {**base_query, "outageState": state}
            try:
                _, resp = self._make_request(
                    "outageRepair/queryPowerCutByRegionCode",
                    query,
                    with_auth=True,
                )
            except CSGAPIError:
                continue
            if resp[JSON_KEY_STA] != RESP_STA_SUCCESS:
                continue
            for item in resp.get(JSON_KEY_DATA, {}).get("infoArray") or []:
                event = {
                    "range": item.get("powFailRanage"),
                    "start_time": item.get("powFailActStartTime"),
                    "end_time": item.get("powFailActEndTime"),
                    "state": item.get("powFailState"),
                    "type": item.get("powFailType"),
                    "is_electric": item.get("isElectric"),
                    "event_id": item.get("powFailEventId"),
                }
                events.append({k: v for k, v in event.items() if v is not None})

        # dedupe by event id and keep only recent events
        seen = set()
        deduped = []
        for event in sorted(
            events, key=lambda e: e.get("start_time") or "", reverse=True
        ):
            event_id = event.get("event_id")
            if event_id and event_id in seen:
                continue
            if event_id:
                seen.add(event_id)
            deduped.append(event)

        def _is_ongoing(event: dict) -> bool:
            end = event.get("end_time")
            if end:
                try:
                    end_dt = datetime.datetime.strptime(
                        end, "%Y-%m-%d %H:%M:%S"
                    )
                    return end_dt >= now - datetime.timedelta(hours=1)
                except ValueError:
                    pass
            return event.get("state") not in ("3",)

        return {
            "region": region,
            "events": deduped,
            "ongoing": sum(1 for e in deduped if _is_ongoing(e)),
        }

    def probe_supported_features(
        self, account: CSGElectricityAccount
    ) -> dict[str, bool]:
        """Detect which features the account's region supports.

        The daily charge api and the ladder info are Shenzhen-first features,
        some regions (e.g. Yunnan) do not have them.
        """
        from .const import CONF_HAS_DAILY_CHARGE, CONF_HAS_LADDER

        features = {
            CONF_HAS_DAILY_CHARGE: False,
            CONF_HAS_LADDER: False,
        }

        # daily charge: probe with last month, which is also the period the
        # coordinator always fetches
        last_month_ym = self._last_month_tuple()
        try:
            self.api_query_day_electric_charge_by_m_point(
                last_month_ym[0],
                last_month_ym[1],
                account.area_code,
                account.ele_customer_id,
                account.metering_point_id,
            )
            features[CONF_HAS_DAILY_CHARGE] = True
        except NotLoggedIn:
            raise
        except CSGAPIError:
            pass

        # ladder: check the ladder fields of the latest available bill
        try:
            bill_detail = self.get_bill_details(account, last_month_ym)
        except NotLoggedIn:
            raise
        except CSGAPIError:
            bill_detail = None
        if bill_detail is not None:
            ladder_marker = (
                bill_detail.get("ladderRate")
                or bill_detail.get("ladderType")
                or bill_detail.get("ladderProject")
            )
            features[CONF_HAS_LADDER] = ladder_marker is not None

        return features

    @staticmethod
    def _last_month_tuple() -> tuple[int, int]:
        """Return (year, month) of last month"""
        now = datetime.datetime.now()
        year, month = now.year, now.month
        if month == 1:
            return year - 1, 12
        return year, month - 1

    # end high-level api wrappers
