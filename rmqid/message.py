"""
The Message class represents a message that is sent or received

"""
import datetime
import logging
import math
import uuid

from pamqp import body
from pamqp import header
from pamqp import specification

from rmqid import base
from rmqid import exceptions

LOGGER = logging.getLogger(__name__)


class Message(base.AMQPClass):
    """Represent a message for delivery and receipt from RabbitMQ"""
    method = None
    name = 'Message'

    def __init__(self, channel, body_value, properties, auto_id=False):
        """Create a new instance of the Message object.

        :param rmqid.channel.Channel channel: The channel object for the message
        :param str body_value: The message body
        :param dict properties: A dictionary of message properties
        :param bool auto_id: Add a message id if no properties were passed in.

        """
        super(Message, self).__init__(channel, 'Message')
        self.body = body_value
        self.properties = properties or self._base_properties
        if auto_id and 'message_id' not in self.properties:
            self._add_auto_message_id()

    def _add_auto_message_id(self):
        """Set the message_id property to a new UUID."""
        self.properties['message_id'] = str(uuid.uuid4())

    @property
    def _base_properties(self):
        """Return a base set of properties if no properties were passed into
        the constructor.

        :rtype: dict

        """
        return {"timestamp": datetime.datetime.now()}

    @property
    def _properties(self):
        """Return a new Basic.Properties object representing the message
        properties.

        :rtype: pamqp.specification.Basic.Properties

        """
        invalid_keys = [key for key in self.properties
                        if key not in specification.Basic.Properties.attributes]
        self._prune_invalid_properties(invalid_keys)
        return specification.Basic.Properties(**self.properties)

    def _prune_invalid_properties(self, invalid_keys):
        """Remove invalid properties from the message properties.

        :param list invalid_keys: A list of invalid property names to remove

        """
        for key in invalid_keys:
            LOGGER.warning('Removing invalid property "%s"', key)
            del self.properties[key]

    def ack(self):
        """Acknowledge receipt of the message to RabbitMQ. Will raise an
        ActionException if the message was not received from a broker.

        :raises: ActionException

        """
        if not self.method:
            raise exceptions.ActionException('Can not ack non-received '
                                             'message')
        LOGGER.debug('Acknowledging delivery tag %s', self.method.delivery_tag)
        basic_ack = specification.Basic.Ack(self.method.delivery_tag)
        LOGGER.debug('Basic.Ack delivery-tag: %s', basic_ack.delivery_tag)
        self.channel.write_frame(basic_ack)

    def nack(self):
        """Negatively acknowledge receipt of the message to RabbitMQ. Will raise
        an ActionException if the message was not received from a broker.

        :raises: ActionException

        """
        if not self.method:
            raise exceptions.ActionException('Can not nack non-received '
                                             'message')
        basic_nack = specification.Basic.Ack(self.method.delivery_tag)
        self.channel.write_frame(basic_nack)

    def publish(self, exchange, routing_key=''):
        """Publish the message to the exchange with the specified routing
        key.

        :param str | rmqid.base.AMQPClass exchange: The exchange to bind to
        :param str routing_key: The routing key to use

        """
        if isinstance(exchange, base.AMQPClass):
            exchange = exchange.name
        method_frame = specification.Basic.Publish(exchange=exchange,
                                                   routing_key=routing_key)
        self.channel.write_frame(method_frame)
        header_frame = header.ContentHeader(body_size=len(self.body),
                                            properties=self._properties)
        self.channel.write_frame(header_frame)
        pieces = int(math.ceil(len(self.body) /
                               float(self.channel.maximum_frame_size)))
        for offset in xrange(0, pieces):
            start = self.channel.maximum_frame_size * offset
            end = start + self.channel.maximum_frame_size
            if end > len(self.body):
                end = len(self.body)
            self.channel.write_frame(body.ContentBody(self.body[start:end]))


    def reject(self):
        """Reject receipt of the message to RabbitMQ. Will raise
        an ActionException if the message was not received from a broker.

        :raises: ActionException

        """
        if not self.method:
            raise exceptions.ActionException('Can not reject non-received '
                                             'message')
        basic_reject = specification.Basic.Reject(self.method.delivery_tag)
        self.channel.write_frame(basic_reject)