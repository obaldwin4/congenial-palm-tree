import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

import marshmallow
import webargs
from eth_utils import to_checksum_address
from marshmallow import Schema, fields, post_load, validates_schema
from marshmallow.exceptions import ValidationError
from werkzeug.datastructures import FileStorage

# Compatibility workaround for newer versions of webargs that don't have compat module
# Define MARSHMALLOW_VERSION_INFO based on the installed marshmallow version
MARSHMALLOW_VERSION_INFO = tuple(int(x) for x in marshmallow.__version__.split('.') if x.isdigit())

from rotkehlchen.accounting.structures import ActionType, LedgerAction, LedgerActionType
from rotkehlchen.assets.asset import Asset
from rotkehlchen.balances.manual import ManuallyTrackedBalance
from rotkehlchen.chain.bitcoin.hdkey import HDKey, XpubType
from rotkehlchen.chain.bitcoin.utils import (
    is_valid_btc_address,
    is_valid_derivation_path,
    scriptpubkey_to_btc_address,
)
from rotkehlchen.chain.ethereum.manager import EthereumManager
from rotkehlchen.chain.ethereum.typing import CustomEthereumToken, UnderlyingToken
from rotkehlchen.chain.substrate.typing import KusamaAddress, SubstratePublicKey
from rotkehlchen.chain.substrate.utils import (
    get_kusama_address_from_public_key,
    is_valid_kusama_address,
)
from rotkehlchen.constants.misc import ZERO
from rotkehlchen.db.settings import ModifiableDBSettings
from rotkehlchen.errors import DeserializationError, EncodingError, UnknownAsset, XPUBError
from rotkehlchen.exchanges.kraken import KrakenAccountType
from rotkehlchen.exchanges.manager import SUPPORTED_EXCHANGES
from rotkehlchen.fval import FVal
from rotkehlchen.history.typing import HistoricalPriceOracle
from rotkehlchen.icons import ALLOWED_ICON_EXTENSIONS
from rotkehlchen.inquirer import CurrentPriceOracle
from rotkehlchen.serialization.deserialize import (
    deserialize_action_type,
    deserialize_asset_amount,
    deserialize_fee,
    deserialize_hex_color_code,
    deserialize_ledger_action_type,
    deserialize_location,
    deserialize_price,
    deserialize_timestamp,
    deserialize_trade_pair,
    deserialize_trade_type,
)
from rotkehlchen.typing import (
    AVAILABLE_MODULES_MAP,
    ApiKey,
    ApiSecret,
    AssetAmount,
    BTCAddress,
    ChecksumEthAddress,
    ExternalService,
    ExternalServiceApiCredentials,
    Fee,
    HexColorCode,
    Location,
    Price,
    SupportedBlockchain,
    Timestamp,
    TradePair,
    TradeType,
)
from rotkehlchen.utils.misc import ts_now

log = logging.getLogger(__name__)


class DelimitedOrNormalList(webargs.fields.DelimitedList):
    """This is equal to DelimitedList in webargs v5.6.0

    Essentially accepting either a delimited string or a list-like object

    We introduce it due to them implementing https://github.com/marshmallow-code/webargs/issues/423
    """

    def __init__(
            self,
            cls_or_instance: Any,
            *,
            _delimiter: Optional[str] = None,
            **kwargs: Any,
    ) -> None:
        super().__init__(cls_or_instance, **kwargs)

    def _deserialize(
            self,
            value: Union[List[str], str],
            attr: str,
            data: Dict[str, Any],
            **kwargs: Any,
    ) -> List[Any]:
        """Adjusting code for _deserialize so that it also works for list-like objects

        Adjusting code from
        https://github.com/marshmallow-code/webargs/blob/dev/src/webargs/fields.py#L71
        so that it uses the list-like detection seen in
        https://github.com/marshmallow-code/webargs/blob/f1ae764973b6492e3c69109060c95240b7cc3d41/src/webargs/fields.py#L69
        which was removed as part of https://github.com/marshmallow-code/webargs/issues/423
        """
        try:
            ret = (
                value
                if marshmallow.utils.is_iterable_but_not_string(value)
                else value.split(self.delimiter)  # type: ignore
            )
        except AttributeError as e:
            if MARSHMALLOW_VERSION_INFO[0] < 3:
                self.fail("invalid")
            else:
                raise self.make_error("invalid") from e
        # purposefully skip the superclass here
        return super(webargs.fields.DelimitedList, self)._deserialize(ret, attr, data, **kwargs)  # pylint: disable=bad-super-call  # noqa: E501


class TimestampField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> Timestamp:
        try:
            timestamp = deserialize_timestamp(value)
        except DeserializationError as e:
            raise ValidationError(str(e)) from e

        return timestamp


class ColorField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> HexColorCode:
        try:
            color_code = deserialize_hex_color_code(value)
        except DeserializationError as e:
            raise ValidationError(str(e)) from e

        return color_code


class TaxFreeAfterPeriodField(fields.Field):

    def _deserialize(
            self,
            value: int,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> int:
        try:
            value = int(value)
        except ValueError:
            raise ValidationError(f'{value} is not a valid integer') from None

        if value < -1:
            raise ValidationError(
                'The taxfree_after_period value can not be negative, except for '
                'the value of -1 to disable the setting',
            )
        if value == 0:
            raise ValidationError('The taxfree_after_period value can not be set to zero')

        return value


class KrakenAccountTypeField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> KrakenAccountType:
        try:
            acc_type = KrakenAccountType.deserialize(value)
        except DeserializationError as e:
            raise ValidationError(f'{value} is not a valid kraken account type') from e

        return acc_type


class AmountField(fields.Field):

    @staticmethod
    def _serialize(
            value: AssetAmount,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value)

    def _deserialize(
            self,
            value: Union[str, int],
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> AssetAmount:
        try:
            amount = deserialize_asset_amount(value)
        except DeserializationError as e:
            raise ValidationError(str(e)) from e

        return amount


class PositiveAmountField(AmountField):

    def _deserialize(
            self,
            value: Union[str, int],
            attr: Optional[str],
            data: Optional[Mapping[str, Any]],
            **kwargs: Any,
    ) -> AssetAmount:
        amount = super()._deserialize(value, attr, data, **kwargs)
        if amount <= ZERO:
            raise ValidationError(f'Non-positive amount {value} given. Amount should be > 0')

        return amount


class PriceField(fields.Field):

    @staticmethod
    def _serialize(
            value: FVal,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value)

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> Price:
        try:
            price = deserialize_price(value)
        except DeserializationError as e:
            raise ValidationError(str(e)) from e

        return price


class FeeField(fields.Field):

    @staticmethod
    def _serialize(
            value: Fee,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value)

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> Fee:
        try:
            fee = deserialize_fee(value)
        except DeserializationError as e:
            raise ValidationError(str(e)) from e

        return fee


class FloatingPercentageField(fields.Field):

    @staticmethod
    def _serialize(
            value: FVal,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value)

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> FVal:
        try:
            percentage = FVal(value)
        except ValueError as e:
            raise ValidationError(str(e)) from e

        if percentage < ZERO:
            raise ValidationError('Percentage field can not be negative')
        if percentage > FVal(100):
            raise ValidationError('Percentage field can not be greater than 100')

        return percentage / FVal(100)


class BlockchainField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> SupportedBlockchain:
        if value in ('btc', 'BTC'):
            return SupportedBlockchain.BITCOIN
        if value in ('eth', 'ETH'):
            return SupportedBlockchain.ETHEREUM
        if value in ('ksm', 'KSM'):
            return SupportedBlockchain.KUSAMA
        raise ValidationError(f'Unrecognized value {value} given for blockchain name')


class AssetField(fields.Field):

    @staticmethod
    def _serialize(
            value: Asset,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value.identifier)

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> Asset:
        try:
            asset = Asset(value)
        except (DeserializationError, UnknownAsset) as e:
            raise ValidationError(str(e)) from e

        return asset


class EthereumAddressField(fields.Field):

    @staticmethod
    def _serialize(
            value: ChecksumEthAddress,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value)

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> ChecksumEthAddress:
        # Make sure that given value is an ethereum address
        try:
            address = to_checksum_address(value)
        except (ValueError, TypeError) as e:
            raise ValidationError(
                f'Given value {value} is not an ethereum address',
                field_name='address',
            ) from e

        return address


class TradeTypeField(fields.Field):

    @staticmethod
    def _serialize(
            value: TradeType,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value)

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> TradeType:
        try:
            trade_type = deserialize_trade_type(value)
        except DeserializationError as e:
            raise ValidationError(str(e)) from e

        return trade_type


class TradePairField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> TradePair:
        if not isinstance(value, str):
            raise ValidationError(f'Provided non-string trade pair value {value}')
        try:
            trade_pair = deserialize_trade_pair(value)
        except DeserializationError as e:
            raise ValidationError(str(e)) from e

        return trade_pair


class LedgerActionTypeField(fields.Field):

    @staticmethod
    def _serialize(
            value: LedgerActionType,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value)

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> LedgerActionType:
        try:
            action_type = deserialize_ledger_action_type(value)
        except DeserializationError as e:
            raise ValidationError(str(e)) from e

        return action_type


class ActionTypeField(fields.Field):

    @staticmethod
    def _serialize(
            value: ActionType,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value)

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> ActionType:
        try:
            action_type = deserialize_action_type(value)
        except DeserializationError as e:
            raise ValidationError(str(e)) from e

        return action_type


class LocationField(fields.Field):

    @staticmethod
    def _serialize(
            value: Location,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value)

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> Location:
        try:
            location = deserialize_location(value)
        except DeserializationError as e:
            raise ValidationError(str(e)) from e

        return location


class ExternalServiceNameField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> ExternalService:
        if not isinstance(value, str):
            raise ValidationError('External service name should be a string')
        service = ExternalService.serialize(value)
        if not service:
            raise ValidationError(f'External service {value} is not known')

        return service


class ExchangeNameField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        if not isinstance(value, str):
            raise ValidationError('Exchange name should be a string')
        if value not in SUPPORTED_EXCHANGES:
            raise ValidationError(f'Exchange {value} is not supported')

        return value


class ApiKeyField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> ApiKey:
        if not isinstance(value, str):
            raise ValidationError('Given API Key should be a string')
        return ApiKey(value)


class ApiSecretField(fields.Field):

    @staticmethod
    def _serialize(
            value: ApiSecret,
            attr: str,  # pylint: disable=unused-argument
            obj: Any,  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        return str(value.decode())

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> ApiSecret:
        if not isinstance(value, str):
            raise ValidationError('Given API Secret should be a string')
        return ApiSecret(value.encode())


class DirectoryField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> Path:
        path = Path(value)
        if not path.exists():
            raise ValidationError(f'Given path {value} does not exist')

        if not path.is_dir():
            raise ValidationError(f'Given path {value} is not a directory')

        return path


class FileField(fields.Field):

    def __init__(self, *, allowed_extensions: Optional[Sequence[str]] = None, **kwargs: Any) -> None:  # noqa: E501
        self.allowed_extensions = allowed_extensions
        super().__init__(**kwargs)

    def _deserialize(
            self,
            value: Union[str, FileStorage],
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> Union[Path, FileStorage]:
        if isinstance(value, FileStorage):
            if self.allowed_extensions is not None and value.filename:
                if not any(value.filename.endswith(x) for x in self.allowed_extensions):
                    raise ValidationError(
                        f'Given file {value.filename} does not end in any of '
                        f'{",".join(self.allowed_extensions)}',
                    )

            return value

        if not isinstance(value, str):
            raise ValidationError('Provided non string or file type for file')

        path = Path(value)
        if not path.exists():
            raise ValidationError(f'Given path {value} does not exist')

        if not path.is_file():
            raise ValidationError(f'Given path {value} is not a file')

        if self.allowed_extensions is not None:
            if not any(path.suffix == x for x in self.allowed_extensions):
                raise ValidationError(
                    f'Given file {path} does not end in any of '
                    f'{",".join(self.allowed_extensions)}',
                )

        return path


class XpubField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> HDKey:
        if not isinstance(value, str):
            raise ValidationError('Xpub should be a string')

        try:
            hdkey = HDKey.from_xpub(value, path='m')
        except XPUBError as e:
            raise ValidationError(str(e)) from e

        return hdkey


class DerivationPathField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> str:
        valid, msg = is_valid_derivation_path(value)
        if not valid:
            raise ValidationError(msg)

        return value


class CurrentPriceOracleField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> CurrentPriceOracle:
        try:
            current_price_oracle = CurrentPriceOracle.deserialize(value)
        except DeserializationError as e:
            raise ValidationError(f'Invalid current price oracle: {value}') from e

        return current_price_oracle


class HistoricalPriceOracleField(fields.Field):

    def _deserialize(
            self,
            value: str,
            attr: Optional[str],  # pylint: disable=unused-argument
            data: Optional[Mapping[str, Any]],  # pylint: disable=unused-argument
            **_kwargs: Any,
    ) -> HistoricalPriceOracle:
        try:
            historical_price_oracle = HistoricalPriceOracle.deserialize(value)
        except DeserializationError as e:
            raise ValidationError(f'Invalid historical price oracle: {value}') from e

        return historical_price_oracle


class AsyncQueryArgumentSchema(Schema):
    """A schema for getters that only have one argument enabling async query"""
    async_query = fields.Boolean(missing=False)


class AsyncHistoricalQuerySchema(AsyncQueryArgumentSchema):
    """A schema for getters that have 2 arguments.
    One to enable async querying and another to force reset DB data by querying everytying again"""
    reset_db_data = fields.Boolean(missing=False)
    from_timestamp = TimestampField(missing=Timestamp(0))
    to_timestamp = TimestampField(missing=ts_now)


class AsyncTasksQuerySchema(Schema):
    task_id = fields.Integer(strict=True, missing=None)


class EthereumTransactionQuerySchema(Schema):
    async_query = fields.Boolean(missing=False)
    address = EthereumAddressField(missing=None)
    from_timestamp = TimestampField(missing=Timestamp(0))
    to_timestamp = TimestampField(missing=ts_now)
    only_cache = fields.Boolean(missing=False)


class TimerangeLocationQuerySchema(Schema):
    from_timestamp = TimestampField(missing=Timestamp(0))
    to_timestamp = TimestampField(missing=ts_now)
    location = LocationField(missing=None)
    async_query = fields.Boolean(missing=False)


class TimerangeLocationCacheQuerySchema(TimerangeLocationQuerySchema):
    only_cache = fields.Boolean(missing=False)


class TradeSchema(Schema):
    timestamp = TimestampField(required=True)
    location = LocationField(required=True)
    pair = TradePairField(required=True)
    trade_type = TradeTypeField(required=True)
    amount = PositiveAmountField(required=True)
    rate = PriceField(required=True)
    fee = FeeField(required=True)
    fee_currency = AssetField(required=True)
    link = fields.String(missing='')
    notes = fields.String(missing='')


class LedgerActionSchema(Schema):
    timestamp = TimestampField(required=True)
    action_type = LedgerActionTypeField(required=True)
    location = LocationField(required=True)
    amount = AmountField(required=True)
    asset = AssetField(required=True)
    link = fields.String(missing='')
    notes = fields.String(missing='')


class LedgerActionWithIdentifierSchema(LedgerActionSchema):
    identifier = fields.Integer(required=True)

    @post_load  # type: ignore
    def make_ledger_action(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> LedgerAction:
        return LedgerAction(**data)


class LedgerActionEditSchema(Schema):
    action = fields.Nested(LedgerActionWithIdentifierSchema, required=True)


class LedgerActionIdentifierSchema(Schema):
    identifier = fields.Integer(required=True)


class ManuallyTrackedBalanceSchema(Schema):
    asset = AssetField(required=True)
    label = fields.String(required=True)
    amount = PositiveAmountField(required=True)
    location = LocationField(required=True)
    tags = fields.List(fields.String(), missing=None)

    @post_load  # type: ignore
    def make_manually_tracked_balances(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> ManuallyTrackedBalance:
        return ManuallyTrackedBalance(**data)


class ManuallyTrackedBalancesSchema(AsyncQueryArgumentSchema):
    balances = fields.List(fields.Nested(ManuallyTrackedBalanceSchema), required=True)


class ManuallyTrackedBalancesDeleteSchema(AsyncQueryArgumentSchema):
    labels = fields.List(fields.String(required=True), required=True)


class TradePatchSchema(TradeSchema):
    trade_id = fields.String(required=True)


class TradeDeleteSchema(Schema):
    trade_id = fields.String(required=True)


class TagSchema(Schema):
    name = fields.String(required=True)
    description = fields.String(missing=None)
    background_color = ColorField(required=True)
    foreground_color = ColorField(required=True)


class TagEditSchema(Schema):
    name = fields.String(required=True)
    description = fields.String(missing=None)
    background_color = ColorField(missing=None)
    foreground_color = ColorField(missing=None)


class TagDeleteSchema(Schema):
    name = fields.String(required=True)


def _validate_current_price_oracles(
        current_price_oracles: List[CurrentPriceOracle],
) -> None:
    """Prevents repeated oracle names and empty list"""
    if (
        len(current_price_oracles) == 0 or
        len(current_price_oracles) != len(set(current_price_oracles))
    ):
        oracle_names = [str(oracle) for oracle in current_price_oracles]
        supported_oracle_names = [str(oracle) for oracle in CurrentPriceOracle]
        raise ValidationError(
            f'Invalid current price oracles in: {", ".join(oracle_names)}. '
            f'Supported oracles are: {", ".join(supported_oracle_names)}. '
            f'Check there are no repeated ones.',
        )


def _validate_historical_price_oracles(
        historical_price_oracles: List[HistoricalPriceOracle],
) -> None:
    """Prevents repeated oracle names and empty list"""
    if (
        len(historical_price_oracles) == 0 or
        len(historical_price_oracles) != len(set(historical_price_oracles))
    ):
        oracle_names = [str(oracle) for oracle in historical_price_oracles]
        supported_oracle_names = [str(oracle) for oracle in HistoricalPriceOracle]
        raise ValidationError(
            f'Invalid historical price oracles in: {", ".join(oracle_names)}. '
            f'Supported oracles are: {", ".join(supported_oracle_names)}. '
            f'Check there are no repeated ones.',
        )


class ModifiableSettingsSchema(Schema):
    """This is the Schema for the settings that can be modified via the API"""
    premium_should_sync = fields.Bool(missing=None)
    include_crypto2crypto = fields.Bool(missing=None)
    anonymized_logs = fields.Bool(missing=None)
    submit_usage_analytics = fields.Bool(missing=None)
    ui_floating_precision = fields.Integer(
        strict=True,
        validate=webargs.validate.Range(
            min=0,
            max=8,
            error='Floating numbers precision in the UI must be between 0 and 8',
        ),
        missing=None,
    )
    taxfree_after_period = TaxFreeAfterPeriodField(missing=None)
    balance_save_frequency = fields.Integer(
        strict=True,
        validate=webargs.validate.Range(
            min=1,
            error='The number of hours after which balances should be saved should be >= 1',
        ),
        missing=None,
    )
    include_gas_costs = fields.Bool(missing=None)
    # TODO: Add some validation to this field
    # even though it gets validated since we try to connect to it
    eth_rpc_endpoint = fields.String(missing=None)
    ksm_rpc_endpoint = fields.String(missing=None)
    main_currency = AssetField(missing=None)
    # TODO: Add some validation to this field
    date_display_format = fields.String(missing=None)
    kraken_account_type = KrakenAccountTypeField(missing=None)
    active_modules = fields.List(fields.String(), missing=None)
    frontend_settings = fields.String(missing=None)
    account_for_assets_movements = fields.Bool(missing=None)
    btc_derivation_gap_limit = fields.Integer(
        strict=True,
        validate=webargs.validate.Range(
            min=1,
            error='The bitcoin address derivation gap limit should be >= 1',
        ),
        missing=None,
    )
    calculate_past_cost_basis = fields.Bool(missing=None)
    display_date_in_localtime = fields.Bool(missing=None)
    current_price_oracles = fields.List(
        CurrentPriceOracleField,
        validate=_validate_current_price_oracles,
        missing=None,
    )
    historical_price_oracles = fields.List(
        HistoricalPriceOracleField,
        validate=_validate_historical_price_oracles,
        missing=None,
    )
    taxable_ledger_actions = fields.List(LedgerActionTypeField, missing=None)

    @validates_schema  # type: ignore
    def validate_settings_schema(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> None:
        if data['active_modules'] is not None:
            for module in data['active_modules']:
                if module not in AVAILABLE_MODULES_MAP:
                    raise ValidationError(
                        message=f'{module} is not a valid module',
                        field_name='active_modules',
                    )

    @post_load  # type: ignore
    def transform_data(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> Any:
        return ModifiableDBSettings(
            premium_should_sync=data['premium_should_sync'],
            include_crypto2crypto=data['include_crypto2crypto'],
            anonymized_logs=data['anonymized_logs'],
            ui_floating_precision=data['ui_floating_precision'],
            taxfree_after_period=data['taxfree_after_period'],
            balance_save_frequency=data['balance_save_frequency'],
            include_gas_costs=data['include_gas_costs'],
            eth_rpc_endpoint=data['eth_rpc_endpoint'],
            ksm_rpc_endpoint=data['ksm_rpc_endpoint'],
            main_currency=data['main_currency'],
            date_display_format=data['date_display_format'],
            submit_usage_analytics=data['submit_usage_analytics'],
            kraken_account_type=data['kraken_account_type'],
            active_modules=data['active_modules'],
            frontend_settings=data['frontend_settings'],
            account_for_assets_movements=data['account_for_assets_movements'],
            btc_derivation_gap_limit=data['btc_derivation_gap_limit'],
            calculate_past_cost_basis=data['calculate_past_cost_basis'],
            display_date_in_localtime=data['display_date_in_localtime'],
            historical_price_oracles=data['historical_price_oracles'],
            current_price_oracles=data['current_price_oracles'],
            taxable_ledger_actions=data['taxable_ledger_actions'],
        )


class EditSettingsSchema(Schema):
    settings = fields.Nested(ModifiableSettingsSchema, required=True)


class BaseUserSchema(Schema):
    name = fields.String(required=True)
    password = fields.String(required=True)


class UserActionSchema(Schema):
    name = fields.String(required=True)
    # All the fields below are not needed for logout/modification so are not required=True
    password = fields.String(missing=None)
    sync_approval = fields.String(
        missing='unknown',
        validate=webargs.validate.OneOf(choices=('unknown', 'yes', 'no')),
    )
    action = fields.String(
        validate=webargs.validate.OneOf(choices=('login', 'logout')),
        missing=None,
    )
    premium_api_key = fields.String(missing='')
    premium_api_secret = fields.String(missing='')

    @validates_schema  # type: ignore
    def validate_user_action_schema(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> None:
        if data['action'] == 'login':
            if data['password'] is None:
                raise ValidationError('Missing password field for login')
        elif data['action'] is None:
            if data['premium_api_key'] == '' or data['premium_api_secret'] == '':
                raise ValidationError(
                    'Without an action premium api key and secret must be provided',
                )


class UserPasswordChangeSchema(Schema):
    name = fields.String(required=True)
    current_password = fields.String(required=True)
    new_password = fields.String(required=True)


class UserPremiumSyncSchema(AsyncQueryArgumentSchema):
    action = fields.String(
        validate=webargs.validate.OneOf(choices=('upload', 'download')),
        required=True,
    )


class NewUserSchema(BaseUserSchema):
    premium_api_key = fields.String(missing='')
    premium_api_secret = fields.String(missing='')
    initial_settings = fields.Nested(ModifiableSettingsSchema, missing=None)


class AllBalancesQuerySchema(Schema):
    async_query = fields.Boolean(missing=False)
    save_data = fields.Boolean(missing=False)
    ignore_cache = fields.Boolean(missing=False)


class ExternalServiceSchema(Schema):
    name = ExternalServiceNameField(required=True)
    api_key = fields.String(required=True)

    @post_load  # type: ignore
    def make_external_service(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> ExternalServiceApiCredentials:
        """Used when encoding an external resource given in via the API"""
        return ExternalServiceApiCredentials(service=data['name'], api_key=data['api_key'])


class ExternalServicesResourceAddSchema(Schema):
    services = fields.List(fields.Nested(ExternalServiceSchema), required=True)


class ExternalServicesResourceDeleteSchema(Schema):
    services = fields.List(ExternalServiceNameField(), required=True)


class ExchangesResourceAddSchema(Schema):
    name = ExchangeNameField(required=True)
    api_key = ApiKeyField(required=True)
    api_secret = ApiSecretField(required=True)
    passphrase = fields.String(missing=None)


class ExchangesDataResourceSchema(Schema):
    name = ExchangeNameField(missing=None)


class ExchangesResourceRemoveSchema(Schema):
    name = ExchangeNameField(required=True)


class ExchangeBalanceQuerySchema(Schema):
    name = ExchangeNameField(missing=None)
    async_query = fields.Boolean(missing=False)
    ignore_cache = fields.Boolean(missing=False)


class BlockchainBalanceQuerySchema(Schema):
    blockchain = BlockchainField(missing=None)
    async_query = fields.Boolean(missing=False)
    ignore_cache = fields.Boolean(missing=False)


class StatisticsAssetBalanceSchema(Schema):
    asset = AssetField(required=True)
    from_timestamp = TimestampField(missing=Timestamp(0))
    to_timestamp = TimestampField(missing=ts_now)


class StatisticsValueDistributionSchema(Schema):
    distribution_by = fields.String(
        required=True,
        validate=webargs.validate.OneOf(choices=('location', 'asset')),
    )


class HistoryProcessingSchema(Schema):
    from_timestamp = TimestampField(missing=Timestamp(0))
    to_timestamp = TimestampField(missing=ts_now)
    async_query = fields.Boolean(missing=False)


class HistoryExportingSchema(Schema):
    directory_path = DirectoryField(required=True)


class BlockchainAccountDataSchema(Schema):
    address = fields.String(required=True)
    label = fields.String(missing=None)
    tags = fields.List(fields.String(), missing=None)


class BaseXpubSchema(Schema):
    xpub = XpubField(required=True)
    derivation_path = DerivationPathField(missing=None)
    async_query = fields.Boolean(missing=False)


class XpubAddSchema(Schema):
    xpub = fields.String(required=True)
    derivation_path = DerivationPathField(missing=None)
    async_query = fields.Boolean(missing=False)
    label = fields.String(missing=None)
    xpub_type = fields.String(
        required=False,
        missing=None,
        validate=webargs.validate.OneOf(choices=('p2pkh', 'p2sh_p2wpkh', 'wpkh')),
    )
    tags = fields.List(fields.String(), missing=None)

    @post_load  # type: ignore
    def transform_data(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> Any:
        xpub_type_str = data.pop('xpub_type', None)
        try:
            xpub_type = None if xpub_type_str is None else XpubType.deserialize(xpub_type_str)
            xpub_hdkey = HDKey.from_xpub(data['xpub'], xpub_type=xpub_type, path='m')
        except (DeserializationError, XPUBError) as e:
            raise ValidationError(
                f'Failed to initialize an xpub due to {str(e)}',
                field_name='xpub',
            ) from e

        data['xpub'] = xpub_hdkey
        return data


class XpubPatchSchema(Schema):
    xpub = XpubField(required=True)
    derivation_path = DerivationPathField(missing=None)
    label = fields.String(missing=None)
    tags = fields.List(fields.String(), missing=None)


class BlockchainAccountsGetSchema(Schema):
    blockchain = BlockchainField(required=True)


def _validate_blockchain_account_schemas(
        data: Dict[str, Any],
        address_getter: Callable,
) -> None:
    """Validates schema input for the PUT/PATCH/DELETE on blockchain account data"""
    # Make sure no duplicates addresses are given
    given_addresses = set()
    # Make sure ethereum addresses are checksummed
    if data['blockchain'] == SupportedBlockchain.ETHEREUM:
        for account_data in data['accounts']:
            address_string = address_getter(account_data)
            if not address_string.endswith('.eth'):
                # Make sure that given value is an ethereum address
                try:
                    address = to_checksum_address(address_string)
                except (ValueError, TypeError) as e:
                    raise ValidationError(
                        f'Given value {address_string} is not an ethereum address',
                        field_name='address',
                    ) from e
            else:
                # else it's ENS name and will be checked in the transformation step and not here
                address = address_string

            if address in given_addresses:
                raise ValidationError(
                    f'Address {address} appears multiple times in the request data',
                    field_name='address',
                )
            given_addresses.add(address)

    # Make sure bitcoin addresses are valid
    elif data['blockchain'] == SupportedBlockchain.BITCOIN:
        for account_data in data['accounts']:
            address = address_getter(account_data)
            # ENS domain will be checked in the transformation step
            if not address.endswith('.eth') and not is_valid_btc_address(address):
                raise ValidationError(
                    f'Given value {address} is not a valid bitcoin address',
                    field_name='address',
                )
            if address in given_addresses:
                raise ValidationError(
                    f'Address {address} appears multiple times in the request data',
                    field_name='address',
                )
            given_addresses.add(address)

    # Make sure kusama addresses are valid (either ss58 format or ENS domain)
    elif data['blockchain'] == SupportedBlockchain.KUSAMA:
        for account_data in data['accounts']:
            address = address_getter(account_data)
            # ENS domain will be checked in the transformation step
            if not address.endswith('.eth') and not is_valid_kusama_address(address):
                raise ValidationError(
                    f'Given value {address} is not a valid kusama address',
                    field_name='address',
                )
            if address in given_addresses:
                raise ValidationError(
                    f'Address {address} appears multiple times in the request data',
                    field_name='address',
                )
            given_addresses.add(address)


def _transform_btc_address(
        ethereum: EthereumManager,
        given_address: str,
) -> BTCAddress:
    """Returns a SegWit/P2PKH/P2SH address (if existing) given an ENS domain.

    NB: ENS domains for BTC store the scriptpubkey. Check EIP-2304.
    """
    if not given_address.endswith('.eth'):
        return BTCAddress(given_address)

    resolved_address = ethereum.ens_lookup(
        given_address,
        blockchain=SupportedBlockchain.BITCOIN,
    )
    if resolved_address is None:
        raise ValidationError(
            f'Given ENS address {given_address} could not be resolved for Bitcoin',
            field_name='address',
        ) from None

    try:
        address = scriptpubkey_to_btc_address(bytes.fromhex(resolved_address))
    except EncodingError as e:
        raise ValidationError(
            f'Given ENS address {given_address} does not contain a valid Bitcoin '
            f"scriptpubkey: {resolved_address}. Bitcoin address can't be obtained.",
            field_name='address',
        ) from e

    log.debug(f'Resolved BTC ENS {given_address} to {address}')

    return address


def _transform_eth_address(
        ethereum: EthereumManager, given_address: str) -> ChecksumEthAddress:
    try:
        address = to_checksum_address(given_address)
    except ValueError:
        # Validation will only let .eth names come here.
        # So let's see if it resolves to anything
        resolved_address = ethereum.ens_lookup(given_address)
        if resolved_address is None:
            raise ValidationError(
                f'Given ENS address {given_address} could not be resolved',
                field_name='address',
            ) from None

        address = to_checksum_address(resolved_address)
        log.info(f'Resolved ENS {given_address} to {address}')

    return address


def _transform_ksm_address(
        ethereum: EthereumManager,
        given_address: str,
) -> KusamaAddress:
    """Returns a KSM address (if exists) given an ENS domain. At this point any
    given address has been already validated either as an ENS name or as a
    valid Kusama address (ss58 format).

    NB: ENS domains for Substrate chains (e.g. KSM, DOT) store the Substrate
    public key. It requires to encode it with a specific ss58 format for
    obtaining the specific chain address.

    Kusama/Polkadot ENS domain accounts:
    https://guide.kusama.network/docs/en/mirror-ens

    ENS domain substrate public key encoding:
    https://github.com/ensdomains/address-encoder/blob/master/src/index.ts
    """
    if not given_address.endswith('.eth'):
        return KusamaAddress(given_address)

    resolved_address = ethereum.ens_lookup(
        given_address,
        blockchain=SupportedBlockchain.KUSAMA,
    )
    if resolved_address is None:
        raise ValidationError(
            f'Given ENS address {given_address} could not be resolved for Kusama',
            field_name='address',
        ) from None

    try:
        address = get_kusama_address_from_public_key(SubstratePublicKey(resolved_address))
    except (TypeError, ValueError) as e:
        raise ValidationError(
            f'Given ENS address {given_address} does not contain a valid '
            f"Substrate public key: {resolved_address}. Kusama address can't be obtained.",
            field_name='address',
        ) from e

    log.debug(f'Resolved KSM ENS {given_address} to {address}')

    return address


class BlockchainAccountsPatchSchema(Schema):
    blockchain = BlockchainField(required=True)
    accounts = fields.List(fields.Nested(BlockchainAccountDataSchema), required=True)

    def __init__(self, ethereum_manager: EthereumManager):
        super().__init__()
        self.ethereum_manager = ethereum_manager

    @validates_schema  # type: ignore
    def validate_schema(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> None:
        _validate_blockchain_account_schemas(data, lambda x: x['address'])

    @post_load  # type: ignore
    def transform_data(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> Any:
        if data['blockchain'] == SupportedBlockchain.BITCOIN:
            for idx, account in enumerate(data['accounts']):
                data['accounts'][idx]['address'] = _transform_btc_address(
                    ethereum=self.ethereum_manager,
                    given_address=account['address'],
                )
        if data['blockchain'] == SupportedBlockchain.ETHEREUM:
            for idx, account in enumerate(data['accounts']):
                data['accounts'][idx]['address'] = _transform_eth_address(
                    ethereum=self.ethereum_manager,
                    given_address=account['address'],
                )
        if data['blockchain'] == SupportedBlockchain.KUSAMA:
            for idx, account in enumerate(data['accounts']):
                data['accounts'][idx]['address'] = _transform_ksm_address(
                    ethereum=self.ethereum_manager,
                    given_address=account['address'],
                )

        return data


class BlockchainAccountsPutSchema(BlockchainAccountsPatchSchema):
    async_query = fields.Boolean(missing=False)


class BlockchainAccountsDeleteSchema(Schema):
    blockchain = BlockchainField(required=True)
    accounts = fields.List(fields.String(), required=True)
    async_query = fields.Boolean(missing=False)

    def __init__(self, ethereum_manager: EthereumManager):
        super().__init__()
        self.ethereum_manager = ethereum_manager

    @validates_schema  # type: ignore
    def validate_blockchain_accounts_delete_schema(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> None:
        _validate_blockchain_account_schemas(data, lambda x: x)

    @post_load  # type: ignore
    def transform_data(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> Any:
        if data['blockchain'] == SupportedBlockchain.BITCOIN:
            data['accounts'] = [
                _transform_btc_address(self.ethereum_manager, x) for x in data['accounts']
            ]
        if data['blockchain'] == SupportedBlockchain.ETHEREUM:
            data['accounts'] = [
                _transform_eth_address(self.ethereum_manager, x) for x in data['accounts']
            ]
        if data['blockchain'] == SupportedBlockchain.KUSAMA:
            data['accounts'] = [
                _transform_ksm_address(self.ethereum_manager, x) for x in data['accounts']
            ]
        return data


class IgnoredAssetsSchema(Schema):
    assets = fields.List(AssetField(), required=True)


class IgnoredActionsGetSchema(Schema):
    action_type = ActionTypeField(missing=None)


class IgnoredActionsModifySchema(Schema):
    action_type = ActionTypeField(required=True)
    action_ids = fields.List(fields.String(required=True), required=True)


class OptionalEthereumAddressSchema(Schema):
    address = EthereumAddressField(required=False, missing=None)


class RequiredEthereumAddressSchema(Schema):
    address = EthereumAddressField(required=True)


class UnderlyingTokenInfoSchema(Schema):
    address = EthereumAddressField(required=True)
    weight = FloatingPercentageField(required=True)


class EthereumTokenSchema(Schema):
    address = EthereumAddressField(required=True)
    decimals = fields.Integer(
        strict=True,
        validate=webargs.validate.Range(
            min=0,
            max=18,
            error='Ethereum token decimals should range from 0 to 18',
        ),
        required=True,
    )
    name = fields.String(required=True)
    symbol = fields.String(required=True)
    started = TimestampField(missing=None)
    coingecko = fields.String(missing=None)
    cryptocompare = fields.String(missing=None)
    underlying_tokens = fields.List(fields.Nested(UnderlyingTokenInfoSchema), missing=None)

    @validates_schema  # type: ignore
    def validate_ethereum_token_schema(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> None:
        given_underlying_tokens = data.get('underlying_tokens', None)
        if given_underlying_tokens is not None:
            if given_underlying_tokens == []:
                raise ValidationError(
                    f'Gave an empty list for underlying tokens of {data["address"]}. '
                    f'If you need to specify no underlying tokens give a null value',
                )
            weight_sum = sum(x['weight'] for x in given_underlying_tokens)
            if weight_sum > FVal(1):
                raise ValidationError(
                    f'The sum of underlying token weights for {data["address"]} '
                    f'is {weight_sum * 100} and exceeds 100%',
                )

    @post_load  # type: ignore
    def transform_data(  # pylint: disable=no-self-use
            self,
            data: Dict[str, Any],
            **_kwargs: Any,
    ) -> CustomEthereumToken:
        given_underlying_tokens = data.pop('underlying_tokens', None)
        underlying_tokens = None
        if given_underlying_tokens is not None:
            underlying_tokens = []
            for entry in given_underlying_tokens:
                underlying_tokens.append(UnderlyingToken(
                    address=entry['address'],
                    weight=entry['weight'],
                ))
        return CustomEthereumToken(**data, underlying_tokens=underlying_tokens)


class ModifyEthereumTokenSchema(Schema):
    token = fields.Nested(EthereumTokenSchema, required=True)


class QueriedAddressesSchema(Schema):
    module = fields.String(
        required=True,
        validate=webargs.validate.OneOf(choices=list(AVAILABLE_MODULES_MAP.keys())),
    )
    address = EthereumAddressField(required=True)


class DataImportSchema(Schema):
    source = fields.String(
        required=True,
        validate=webargs.validate.OneOf(choices=('cointracking.info', 'crypto.com')),
    )
    file = FileField(required=True, allowed_extensions=('.csv',))


class AssetIconUploadSchema(Schema):
    asset = AssetField(required=True)
    file = FileField(required=True, allowed_extensions=ALLOWED_ICON_EXTENSIONS)


class ExchangeRatesSchema(Schema):
    currencies = DelimitedOrNormalList(AssetField(), required=True)


class WatcherSchema(Schema):
    type = fields.String(required=True)
    args = fields.Dict(required=True)


class WatchersAddSchema(Schema):
    """The schema for adding a watcher.

    No validation here since it happens server side and no need to duplicate code
    TODO: When we have common libraries perhaps do validation here too to
    avoid potential server roundtrip for nothing
    """
    watchers = fields.List(fields.Nested(WatcherSchema), required=True)


class WatcherForEditingSchema(WatcherSchema):
    identifier = fields.String(required=True)


class WatchersEditSchema(WatchersAddSchema):
    """The schema for editing a watcher.

    No validation here since it happens server side and no need to duplicate code
    TODO: When we have common libraries perhaps do validation here too to
    avoid potential server roundtrip for nothing
    """
    watchers = fields.List(fields.Nested(WatcherForEditingSchema), required=True)


class WatchersDeleteSchema(Schema):
    """The schema for deleting watchers.

    No validation here since it happens server side and no need to duplicate code
    TODO: When we have common libraries perhaps do validation here too to
    avoid potential server roundtrip for nothing
    """
    watchers = fields.List(fields.String(required=True), required=True)


class AssetIconsSchema(Schema):
    asset = AssetField(required=True)
    size = fields.String(
        validate=webargs.validate.OneOf(choices=('thumb', 'small', 'large')),
        missing='thumb',
    )


class CurrentAssetsPriceSchema(Schema):
    assets = DelimitedOrNormalList(
        AssetField(required=True),
        required=True,
        validate=webargs.validate.Length(min=1),
    )
    target_asset = AssetField(required=True)
    ignore_cache = fields.Boolean(missing=False)
    async_query = fields.Boolean(missing=False)


class HistoricalAssetsPriceSchema(Schema):
    assets_timestamp = fields.List(
        fields.Tuple(  # type: ignore # Tuple is not annotated
            (AssetField(required=True), TimestampField(required=True)),
            required=True,
        ),
        required=True,
        validate=webargs.validate.Length(min=1),
    )
    target_asset = AssetField(required=True)
    async_query = fields.Boolean(missing=False)


class NamedEthereumModuleDataSchema(Schema):
    module_name = fields.String(
        validate=webargs.validate.OneOf(choices=list(AVAILABLE_MODULES_MAP.keys())),
    )


class NamedOracleCacheSchema(Schema):
    oracle = HistoricalPriceOracleField(required=True)
    from_asset = AssetField(required=True)
    to_asset = AssetField(required=True)


class NamedOracleCacheCreateSchema(NamedOracleCacheSchema):
    purge_old = fields.Boolean(missing=False)
    async_query = fields.Boolean(missing=False)


class NamedOracleCacheGetSchema(AsyncQueryArgumentSchema):
    oracle = HistoricalPriceOracleField(required=True)
