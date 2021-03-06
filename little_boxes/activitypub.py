"""Core ActivityPub classes."""
import json
import logging
import weakref
from datetime import datetime
from enum import Enum
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Type
from typing import Union

from .backend import Backend
from .collection import parse_collection
from .errors import BadActivityError
from .errors import Error
from .errors import NotFromOutboxError
from .errors import UnexpectedActivityTypeError

logger = logging.getLogger(__name__)

UninitializedBackendError = Error("a backend must be initialized")

# Helper/shortcut for typing
ObjectType = Dict[str, Any]
ObjectOrIDType = Union[str, ObjectType]

CTX_AS = "https://www.w3.org/ns/activitystreams"
CTX_SECURITY = "https://w3id.org/security/v1"
AS_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"

COLLECTION_CTX = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
    {"Hashtag": "as:Hashtag", "sensitive": "as:sensitive"},
]

# Will be used to keep track of all the defined activities
_ACTIVITY_CLS: Dict["ActivityType", Type["BaseActivity"]] = {}

BACKEND: Optional[Backend] = None


def get_backend() -> Backend:
    if BACKEND is None:
        raise UninitializedBackendError
    return BACKEND


def use_backend(backend_instance):
    global BACKEND
    BACKEND = backend_instance


class ActivityType(Enum):
    """Supported activity `type`."""

    ANNOUNCE = "Announce"
    BLOCK = "Block"
    LIKE = "Like"
    CREATE = "Create"
    UPDATE = "Update"
    PERSON = "Person"
    ORDERED_COLLECTION = "OrderedCollection"
    ORDERED_COLLECTION_PAGE = "OrderedCollectionPage"
    COLLECTION_PAGE = "CollectionPage"
    COLLECTION = "Collection"
    NOTE = "Note"
    ACCEPT = "Accept"
    REJECT = "Reject"
    FOLLOW = "Follow"
    DELETE = "Delete"
    UNDO = "Undo"
    IMAGE = "Image"
    TOMBSTONE = "Tombstone"


def parse_activity(
    payload: ObjectType, expected: Optional[ActivityType] = None
) -> "BaseActivity":
    t = ActivityType(payload["type"])

    if expected and t != expected:
        raise UnexpectedActivityTypeError(
            f'expected a {expected.name} activity, got a {payload["type"]}'
        )

    if t not in _ACTIVITY_CLS:
        raise BadActivityError(f'unsupported activity type {payload["type"]}')

    activity = _ACTIVITY_CLS[t](**payload)

    return activity


def _to_list(data: Union[List[Any], Any]) -> List[Any]:
    """Helper to convert fields that can be either an object or a list of objects to a
    list of object."""
    if isinstance(data, list):
        return data
    return [data]


def clean_activity(activity: ObjectType) -> Dict[str, Any]:
    """Clean the activity before rendering it.
     - Remove the hidden bco and bcc field
    """
    for field in ["bto", "bcc"]:
        if field in activity:
            del (activity[field])
        if activity["type"] == "Create" and field in activity["object"]:
            del (activity["object"][field])
    return activity


def _get_actor_id(actor: ObjectOrIDType) -> str:
    """Helper for retrieving an actor `id`."""
    if isinstance(actor, dict):
        return actor["id"]
    return actor


class _ActivityMeta(type):
    """Metaclass for keeping track of subclass."""

    def __new__(meta, name, bases, class_dict):
        cls = type.__new__(meta, name, bases, class_dict)

        # Ensure the class has an activity type defined
        if name != "BaseActivity" and not cls.ACTIVITY_TYPE:
            raise ValueError(f"class {name} has no ACTIVITY_TYPE")

        # Register it
        _ACTIVITY_CLS[cls.ACTIVITY_TYPE] = cls
        return cls


class BaseActivity(object, metaclass=_ActivityMeta):
    """Base class for ActivityPub activities."""

    ACTIVITY_TYPE: Optional[
        ActivityType
    ] = None  # the ActivityTypeEnum the class will represent
    OBJECT_REQUIRED = False  # Whether the object field is required or note
    ALLOWED_OBJECT_TYPES: List[ActivityType] = []
    ACTOR_REQUIRED = (
        True
    )  # Most of the object requires an actor, so this flag in on by default

    def __init__(self, **kwargs) -> None:  # noqa: C901
        if not self.ACTIVITY_TYPE:
            raise Error("should never happen")

        if kwargs.get("type") and kwargs.pop("type") != self.ACTIVITY_TYPE.value:
            raise UnexpectedActivityTypeError(
                f"Expect the type to be {self.ACTIVITY_TYPE.value!r}"
            )

        # Initialize the dict that will contains all the activity fields
        self._data: Dict[str, Any] = {"type": self.ACTIVITY_TYPE.value}
        logger.debug(f"initializing a {self.ACTIVITY_TYPE.value} activity: {kwargs!r}")

        # A place to set ephemeral data
        self.__ctx: Any = {}

        # The id may not be present for new activities
        if "id" in kwargs:
            self._data["id"] = kwargs.pop("id")

        if self.ACTIVITY_TYPE != ActivityType.PERSON and self.ACTOR_REQUIRED:
            actor = kwargs.get("actor")
            if actor:
                kwargs.pop("actor")
                actor = self._validate_person(actor)
                self._data["actor"] = actor
            elif self.ACTIVITY_TYPE == ActivityType.NOTE:
                if "attributedTo" not in kwargs:
                    raise BadActivityError(f"Note is missing attributedTo")
            else:
                raise BadActivityError("missing actor")

        if self.OBJECT_REQUIRED and "object" in kwargs:
            obj = kwargs.pop("object")
            if isinstance(obj, str):
                # The object is a just a reference the its ID/IRI
                # FIXME(tsileo): fetch the ref
                self._data["object"] = obj
            else:
                if not self.ALLOWED_OBJECT_TYPES:
                    raise UnexpectedActivityTypeError("unexpected object")
                if "type" not in obj or (
                    self.ACTIVITY_TYPE != ActivityType.CREATE and "id" not in obj
                ):
                    raise BadActivityError("invalid object, missing type")
                if ActivityType(obj["type"]) not in self.ALLOWED_OBJECT_TYPES:
                    raise UnexpectedActivityTypeError(
                        f'unexpected object type {obj["type"]} (allowed={self.ALLOWED_OBJECT_TYPES!r})'
                    )
                self._data["object"] = obj

        if "@context" not in kwargs:
            self._data["@context"] = CTX_AS
        else:
            self._data["@context"] = kwargs.pop("@context")

        # @context check
        if not isinstance(self._data["@context"], list):
            self._data["@context"] = [self._data["@context"]]
        if CTX_SECURITY not in self._data["@context"]:
            self._data["@context"].append(CTX_SECURITY)
        if isinstance(self._data["@context"][-1], dict):
            self._data["@context"][-1]["Hashtag"] = "as:Hashtag"
            self._data["@context"][-1]["sensitive"] = "as:sensitive"
        else:
            self._data["@context"].append(
                {"Hashtag": "as:Hashtag", "sensitive": "as:sensitive"}
            )

        # FIXME(tsileo): keys required for some subclasses?
        allowed_keys = None
        try:
            allowed_keys = self._init(**kwargs)
            logger.debug("calling custom init")
        except NotImplementedError:
            pass

        if allowed_keys:
            # Allows an extra to (like for Accept and Follow)
            kwargs.pop("to", None)
            if len(set(kwargs.keys()) - set(allowed_keys)) > 0:
                raise BadActivityError(f"extra data left: {kwargs!r}")
        else:
            # Remove keys with `None` value
            valid_kwargs = {}
            for k, v in kwargs.items():
                if v is None:
                    continue
                valid_kwargs[k] = v
            self._data.update(**valid_kwargs)

    def ctx(self) -> Any:
        return self.__ctx()

    def set_ctx(self, ctx: Any) -> None:
        # FIXME(tsileo): does not use the ctx to set the id to the "parent" when building  delete
        self.__ctx = weakref.ref(ctx)

    def _init(self, **kwargs) -> Optional[List[str]]:
        """Optional init callback that may returns a list of allowed keys."""
        raise NotImplementedError

    def __repr__(self) -> str:
        """Pretty repr."""
        return "{}({!r})".format(self.__class__.__qualname__, self._data.get("id"))

    def __str__(self) -> str:
        """Returns the ID/IRI when castign to str."""
        return str(self._data.get("id", f"[new {self.ACTIVITY_TYPE} activity]"))

    def __getattr__(self, name: str) -> Any:
        """Allow to access the object field as regular attributes."""
        if self._data.get(name):
            return self._data.get(name)

    def _outbox_set_id(self, uri: str, obj_id: str) -> None:
        """Optional callback for subclasses to so something with a newly generated ID (for outbox activities)."""
        raise NotImplementedError

    def outbox_set_id(self, uri: str, obj_id: str) -> None:
        """Set the ID for a new activity."""
        logger.debug(f"setting ID {uri} / {obj_id}")
        self._data["id"] = uri
        try:
            self._outbox_set_id(uri, obj_id)
        except NotImplementedError:
            pass

    def _actor_id(self, obj: ObjectOrIDType) -> str:
        if isinstance(obj, dict) and obj["type"] == ActivityType.PERSON.value:
            obj_id = obj.get("id")
            if not obj_id:
                raise BadActivityError(f"missing object id: {obj!r}")
            return obj_id
        elif isinstance(obj, str):
            return obj
        else:
            raise BadActivityError(f'invalid "actor" field: {obj!r}')

    def _validate_person(self, obj: ObjectOrIDType) -> str:
        if BACKEND is None:
            raise UninitializedBackendError

        obj_id = self._actor_id(obj)
        try:
            actor = BACKEND.fetch_iri(obj_id)
        except Exception:
            raise BadActivityError(f"failed to validate actor {obj!r}")

        if not actor or "id" not in actor:
            raise BadActivityError(f"invalid actor {actor}")

        return actor["id"]

    def get_object(self) -> "BaseActivity":
        """Returns the object as a BaseActivity instance."""
        if BACKEND is None:
            raise UninitializedBackendError

        if self.__obj:
            return self.__obj
        if isinstance(self._data["object"], dict):
            p = parse_activity(self._data["object"])
        else:
            obj = BACKEND.fetch_iri(self._data["object"])
            if ActivityType(obj.get("type")) not in self.ALLOWED_OBJECT_TYPES:
                raise UnexpectedActivityTypeError(
                    f'invalid object type {obj.get("type")!r}'
                )
            p = parse_activity(obj)

        self.__obj: Optional["BaseActivity"] = p
        return p

    def reset_object_cache(self) -> None:
        self.__obj = None

    def to_dict(
        self, embed: bool = False, embed_object_id_only: bool = False
    ) -> ObjectType:
        """Serializes the activity back to a dict, ready to be JSON serialized."""
        data = dict(self._data)
        if embed:
            for k in ["@context", "signature"]:
                if k in data:
                    del (data[k])
        if (
            data.get("object")
            and embed_object_id_only
            and isinstance(data["object"], dict)
        ):
            try:
                data["object"] = data["object"]["id"]
            except KeyError:
                raise BadActivityError(
                    f'embedded object {data["object"]!r} should have an id'
                )

        return data

    def get_actor(self) -> "Person":
        if BACKEND is None:
            raise UninitializedBackendError

        # FIXME(tsileo): cache the actor (same way as get_object)
        actor = self._data.get("actor")
        if not actor and self.ACTOR_REQUIRED:
            # Quick hack for Note objects
            if self.ACTIVITY_TYPE == ActivityType.NOTE:
                actor = str(self._data.get("attributedTo"))
            else:
                raise BadActivityError(f"failed to fetch actor: {self._data!r}")

        if not isinstance(actor, (str, dict)):
            raise BadActivityError(f"invalid actor: {self._data!r}")

        actor_id = self._actor_id(actor)
        return Person(**BACKEND.fetch_iri(actor_id))

    def _pre_post_to_outbox(self) -> None:
        raise NotImplementedError

    def _post_to_outbox(
        self,
        as_actor: "Person",
        obj_id: str,
        activity: ObjectType,
        recipients: List[str],
    ) -> None:
        raise NotImplementedError

    def _undo_outbox(self, as_actor: "Person") -> None:
        raise NotImplementedError

    def _pre_process_from_inbox(self, as_actor: "Person") -> None:
        raise NotImplementedError

    def _process_from_inbox(self, as_actor: "Person") -> None:
        raise NotImplementedError

    def _undo_inbox(self, as_actor: "Person") -> None:
        raise NotImplementedError

    def process_from_inbox(self, as_actor: "Person") -> None:
        """Process the message posted to `as_actor` inbox."""
        if BACKEND is None:
            raise UninitializedBackendError

        logger.debug(f"calling main process from inbox hook for {self}")
        actor = self.get_actor()

        # Check for Block activity
        if BACKEND.outbox_is_blocked(as_actor, actor.id):
            # TODO(tsileo): raise ActorBlockedError?
            logger.info(
                f"actor {actor!r} is blocked, dropping the received activity {self!r}"
            )
            return

        if BACKEND.inbox_get_by_iri(as_actor, self.id):
            # The activity is already in the inbox
            logger.info(f"received duplicate activity {self}, dropping it")
            return

        try:
            self._pre_process_from_inbox(as_actor)
            logger.debug("called pre process from inbox hook")
        except NotImplementedError:
            logger.debug("pre process from inbox hook not implemented")

        BACKEND.inbox_new(as_actor, self)
        logger.info("activity {self!r} saved")

        try:
            self._process_from_inbox(as_actor)
            logger.debug("called process from inbox hook")
        except NotImplementedError:
            logger.debug("process from inbox hook not implemented")

    def post_to_outbox(self) -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        logger.debug(f"calling main post to outbox hook for {self}")

        # Assign create a random ID
        obj_id = BACKEND.random_object_id()
        self.outbox_set_id(BACKEND.activity_url(obj_id), obj_id)

        try:
            self._pre_post_to_outbox()
            logger.debug(f"called pre post to outbox hook")
        except NotImplementedError:
            logger.debug("pre post to outbox hook not implemented")

        BACKEND.outbox_new(self.get_actor(), self)

        recipients = self.recipients()
        logger.info(f"recipients={recipients}")
        activity = clean_activity(self.to_dict())

        try:
            self._post_to_outbox(self.get_actor(), obj_id, activity, recipients)
            logger.debug(f"called post to outbox hook")
        except NotImplementedError:
            logger.debug("post to outbox hook not implemented")

        payload = json.dumps(activity)
        for recp in recipients:
            logger.debug(f"posting to {recp}")

            BACKEND.post_to_remote_inbox(self.get_actor(), payload, recp)

    def _recipients(self) -> List[str]:
        return []

    def recipients(self) -> List[str]:  # noqa: C901
        if BACKEND is None:
            raise UninitializedBackendError

        recipients = self._recipients()
        actor_id = self.get_actor().id

        out: List[str] = []
        for recipient in recipients:
            # if recipient in PUBLIC_INSTANCES:
            #    if recipient not in out:
            #        out.append(str(recipient))
            #    continue
            if recipient in [actor_id, AS_PUBLIC, None]:
                continue
            if isinstance(recipient, Person):
                if recipient.id == actor_id:
                    continue
                actor = recipient
            else:
                raw_actor = BACKEND.fetch_iri(recipient)
                if raw_actor["type"] == ActivityType.PERSON.value:
                    actor = Person(**raw_actor)

                    if actor.endpoints:
                        shared_inbox = actor.endpoints.get("sharedInbox")
                        if shared_inbox not in out:
                            out.append(shared_inbox)
                            continue

                    if actor.inbox and actor.inbox not in out:
                        out.append(actor.inbox)

                # Is the activity a `Collection`/`OrderedCollection`?
                elif raw_actor["type"] in [
                    ActivityType.COLLECTION.value,
                    ActivityType.ORDERED_COLLECTION.value,
                ]:
                    for item in parse_collection(raw_actor, fetcher=BACKEND.fetch_iri):
                        if item in [actor_id, AS_PUBLIC]:
                            continue
                        try:
                            col_actor = Person(**BACKEND.fetch_iri(item))
                        except UnexpectedActivityTypeError:
                            logger.exception(f"failed to fetch actor {item!r}")

                        if col_actor.endpoints:
                            shared_inbox = col_actor.endpoints.get("sharedInbox")
                            if shared_inbox not in out:
                                out.append(shared_inbox)
                                continue
                        if col_actor.inbox and col_actor.inbox not in out:
                            out.append(col_actor.inbox)
                else:
                    raise BadActivityError(f"failed to parse {raw_actor!r}")

        return out

    def build_undo(self) -> "BaseActivity":
        raise NotImplementedError

    def build_delete(self) -> "BaseActivity":
        raise NotImplementedError


class Person(BaseActivity):
    ACTIVITY_TYPE = ActivityType.PERSON
    OBJECT_REQUIRED = False
    ACTOR_REQUIRED = False


class Block(BaseActivity):
    ACTIVITY_TYPE = ActivityType.BLOCK
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True


class Collection(BaseActivity):
    ACTIVITY_TYPE = ActivityType.COLLECTION
    OBJECT_REQUIRED = False
    ACTOR_REQUIRED = False


class Image(BaseActivity):
    ACTIVITY_TYPE = ActivityType.IMAGE
    OBJECT_REQUIRED = False
    ACTOR_REQUIRED = False

    def _init(self, **kwargs):
        self._data.update(url=kwargs.pop("url"))

    def __repr__(self):
        return "Image({!r})".format(self._data.get("url"))


class Follow(BaseActivity):
    ACTIVITY_TYPE = ActivityType.FOLLOW
    ALLOWED_OBJECT_TYPES = [ActivityType.PERSON]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True

    def _build_reply(self, reply_type: ActivityType) -> BaseActivity:
        if reply_type == ActivityType.ACCEPT:
            return Accept(actor=self.get_object().id, object=self.to_dict(embed=True))

        raise ValueError(f"type {reply_type} is invalid for building a reply")

    def _recipients(self) -> List[str]:
        return [self.get_object().id]

    def _process_from_inbox(self, as_actor: "Person") -> None:
        """Receiving a Follow should trigger an Accept."""
        if BACKEND is None:
            raise UninitializedBackendError

        accept = self.build_accept()
        accept.post_to_outbox()

        BACKEND.new_follower(as_actor, self)

    def _post_to_outbox(
        self,
        as_actor: "Person",
        obj_id: str,
        activity: ObjectType,
        recipients: List[str],
    ) -> None:
        # XXX The new_following event will be triggered by Accept
        pass

    def _undo_inbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.undo_new_follower(as_actor, self)

    def _undo_outbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.undo_new_following(as_actor, self)

    def build_accept(self) -> BaseActivity:
        return self._build_reply(ActivityType.ACCEPT)

    def build_undo(self) -> BaseActivity:
        return Undo(object=self.to_dict(embed=True), actor=self.get_actor().id)


class Accept(BaseActivity):
    ACTIVITY_TYPE = ActivityType.ACCEPT
    ALLOWED_OBJECT_TYPES = [ActivityType.FOLLOW]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True

    def _recipients(self) -> List[str]:
        return [self.get_object().get_actor().id]

    def _pre_process_from_inbox(self, as_actor: "Person") -> None:
        # FIXME(tsileo): ensure the actor match the object actor
        pass

    def _process_from_inbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        o = self.get_object()
        if isinstance(o, Follow):
            BACKEND.new_following(as_actor, o)


class Undo(BaseActivity):
    ACTIVITY_TYPE = ActivityType.UNDO
    ALLOWED_OBJECT_TYPES = [
        ActivityType.FOLLOW,
        ActivityType.LIKE,
        ActivityType.ANNOUNCE,
    ]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True

    def _recipients(self) -> List[str]:
        obj = self.get_object()
        if obj.ACTIVITY_TYPE == ActivityType.FOLLOW:
            return [obj.get_object().id]
        else:
            return [obj.get_object().get_actor().id]
            # TODO(tsileo): handle like and announce
            raise Exception("TODO")

    def _pre_process_from_inbox(self, as_actor: "Person") -> None:
        """Ensures an Undo activity comes from the same actor as the updated activity."""
        obj = self.get_object()
        actor = self.get_actor()
        if actor.id != obj.get_actor().id:
            raise BadActivityError(f"{actor!r} cannot update {obj!r}")

    def _process_from_inbox(self, as_actor: "Person") -> None:
        obj = self.get_object()
        # FIXME(tsileo): move this to _undo_inbox impl
        # DB.inbox.update_one({'remote_id': obj.id}, {'$set': {'meta.undo': True}})

        try:
            obj._undo_inbox(as_actor)
        except NotImplementedError:
            pass

    def _pre_post_to_outbox(self) -> None:
        """Ensures an Undo activity references an activity owned by the instance."""
        if BACKEND is None:
            raise UninitializedBackendError

        if not BACKEND.is_from_outbox(self):
            raise NotFromOutboxError(f"object {self!r} is not owned by this instance")

    def _post_to_outbox(
        self,
        as_actor: "Person",
        obj_id: str,
        activity: ObjectType,
        recipients: List[str],
    ) -> None:
        logger.debug("processing undo to outbox")
        logger.debug("self={}".format(self))
        obj = self.get_object()
        logger.debug("obj={}".format(obj))

        # FIXME(tsileo): move this to _undo_inbox impl
        # DB.outbox.update_one({'remote_id': obj.id}, {'$set': {'meta.undo': True}})

        try:
            obj._undo_outbox(as_actor)
            logger.debug(f"_undo_outbox called for {obj}")
        except NotImplementedError:
            logger.debug(f"_undo_outbox not implemented for {obj}")
            pass


class Like(BaseActivity):
    ACTIVITY_TYPE = ActivityType.LIKE
    ALLOWED_OBJECT_TYPES = [ActivityType.NOTE]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True

    def _recipients(self) -> List[str]:
        return [self.get_object().get_actor().id]

    def _process_from_inbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.inbox_like(as_actor, self)

    def _undo_inbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.inbox_undo_like(as_actor, self)

    def _post_to_outbox(
        self,
        as_actor: "Person",
        obj_id: str,
        activity: ObjectType,
        recipients: List[str],
    ):
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.outbox_like(as_actor, self)

    def _undo_outbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.outbox_undo_like(as_actor, self)

    def build_undo(self) -> BaseActivity:
        return Undo(
            object=self.to_dict(embed=True, embed_object_id_only=True),
            actor=self.get_actor().id,
        )


class Announce(BaseActivity):
    ACTIVITY_TYPE = ActivityType.ANNOUNCE
    ALLOWED_OBJECT_TYPES = [ActivityType.NOTE]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True

    def _recipients(self) -> List[str]:
        recipients = [self.get_object().get_actor().id]

        for field in ["to", "cc"]:
            if field in self._data:
                recipients.extend(_to_list(self._data[field]))

        return list(set(recipients))

    def _process_from_inbox(self, as_actor: "Person") -> None:
        # XXX(tsileo): Mastodon will try to send Announce for OStatus only acitivities which we cannot parse
        if isinstance(self._data["object"], str) and not self._data[
            "object"
        ].startswith("http"):
            # TODO(tsileo): actually drop it without storing it and better logging, also move the check somewhere else
            logger.warn(
                f'received an Annouce referencing an OStatus notice ({self._data["object"]}), dropping the message'
            )
            return

        BACKEND.inbox_announce(as_actor, self)

    def _undo_inbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.inbox_undo_announce(as_actor, self)

    def _post_to_outbox(
        self,
        as_actor: "Person",
        obj_id: str,
        activity: ObjectType,
        recipients: List[str],
    ) -> None:
        BACKEND.outbox_announce(as_actor, self)

    def _undo_outbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.outbox_undo_announce(as_actor, self)

    def build_undo(self) -> BaseActivity:
        return Undo(actor=self.get_actor().id, object=self.to_dict(embed=True))


class Delete(BaseActivity):
    ACTIVITY_TYPE = ActivityType.DELETE
    ALLOWED_OBJECT_TYPES = [ActivityType.NOTE, ActivityType.TOMBSTONE]
    OBJECT_REQUIRED = True

    def _get_actual_object(self) -> BaseActivity:
        if BACKEND is None:
            raise UninitializedBackendError

        # FIXME(tsileo): overrides get_object instead?
        obj = self.get_object()
        if obj.ACTIVITY_TYPE == ActivityType.TOMBSTONE:
            obj = parse_activity(BACKEND.fetch_iri(obj.id))
        return obj

    def _recipients(self) -> List[str]:
        obj = self._get_actual_object()
        return obj._recipients()

    def _pre_process_from_inbox(self, as_actor: "Person") -> None:
        """Ensures a Delete activity comes from the same actor as the deleted activity."""
        obj = self._get_actual_object()
        actor = self.get_actor()
        if actor.id != obj.get_actor().id:
            raise BadActivityError(f"{actor!r} cannot delete {obj!r}")

    def _process_from_inbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.inbox_delete(as_actor, self)
        # FIXME(tsileo): handle the delete_threads here?

    def _pre_post_to_outbox(self) -> None:
        """Ensures the Delete activity references a activity from the outbox (i.e. owned by the instance)."""
        if BACKEND is None:
            raise UninitializedBackendError

        obj = self._get_actual_object()

        if not BACKEND.is_from_outbox(self):
            raise NotFromOutboxError(
                f'object {obj["id"]} is not owned by this instance'
            )

    def _post_to_outbox(
        self,
        as_actor: "Person",
        obj_id: str,
        activity: ObjectType,
        recipients: List[str],
    ) -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.outbox_delete(as_actor, self)


class Update(BaseActivity):
    ACTIVITY_TYPE = ActivityType.UPDATE
    ALLOWED_OBJECT_TYPES = [ActivityType.NOTE, ActivityType.PERSON]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True

    def _pre_process_from_inbox(self, as_actor: "Person") -> None:
        """Ensures an Update activity comes from the same actor as the updated activity."""
        obj = self.get_object()
        actor = self.get_actor()
        if actor.id != obj.get_actor().id:
            raise BadActivityError(f"{actor!r} cannot update {obj!r}")

    def _process_from_inbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.inbox_update(as_actor, self)

    def _pre_post_to_outbox(self) -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        if not BACKEND.is_from_outbox(self):
            raise NotFromOutboxError(f"object {self!r} is not owned by this instance")

    def _post_to_outbox(
        self,
        as_actor: "Person",
        obj_id: str,
        activity: ObjectType,
        recipients: List[str],
    ) -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.outbox_update(as_actor, self)


class Create(BaseActivity):
    ACTIVITY_TYPE = ActivityType.CREATE
    ALLOWED_OBJECT_TYPES = [ActivityType.NOTE]
    OBJECT_REQUIRED = True
    ACTOR_REQUIRED = True

    def _outbox_set_id(self, uri: str, obj_id: str) -> None:
        self._data["object"]["id"] = uri + "/activity"
        if isinstance(self.ctx(), Note):
            try:
                print("SETTING ID")
                # FIXME(tsileo): use a weakref instead of ctx, and make it generic to every object (when
                # building things (and drop the set_ctx usage)
                self.ctx().id = self._data["object"]["id"]
                print(f"CTX {self.ctx()}")
            except NotImplementedError:
                pass
        # FIXME(tsileo): re-enable this
        # self._data["object"]["url"] = self.note_url(self)
        self.reset_object_cache()

    def _init(self, **kwargs):
        obj = self.get_object()
        if not obj.attributedTo:
            self._data["object"]["attributedTo"] = self.get_actor().id
        if not obj.published:
            if self.published:
                self._data["object"]["published"] = self.published
            else:
                now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
                self._data["published"] = now
                self._data["object"]["published"] = now

    def _recipients(self) -> List[str]:
        # TODO(tsileo): audience support?
        recipients = []
        for field in ["to", "cc", "bto", "bcc"]:
            if field in self._data:
                recipients.extend(_to_list(self._data[field]))

        recipients.extend(self.get_object()._recipients())

        return recipients

    def _process_from_inbox(self, as_actor: "Person") -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.inbox_create(as_actor, self)

    def _post_to_outbox(
        self,
        as_actor: "Person",
        obj_id: str,
        activity: ObjectType,
        recipients: List[str],
    ) -> None:
        if BACKEND is None:
            raise UninitializedBackendError

        BACKEND.outbox_create(as_actor, self)


class Tombstone(BaseActivity):
    ACTIVITY_TYPE = ActivityType.TOMBSTONE
    ACTOR_REQUIRED = False
    OBJECT_REQUIRED = False


class Note(BaseActivity):
    ACTIVITY_TYPE = ActivityType.NOTE
    ACTOR_REQUIRED = True
    OBJECT_REQURIED = False

    def _init(self, **kwargs):
        if "sensitive" not in kwargs:
            self._data["sensitive"] = False

    def _recipients(self) -> List[str]:
        # TODO(tsileo): audience support?
        recipients: List[str] = []

        # FIXME(tsileo): re-add support for the PUBLIC_INSTANCES
        # If the note is public, we publish it to the defined "public instances"
        # if AS_PUBLIC in self._data.get('to', []):
        #    recipients.extend(PUBLIC_INSTANCES)
        #    print('publishing to public instances')
        #    print(recipients)

        for field in ["to", "cc", "bto", "bcc"]:
            if field in self._data:
                recipients.extend(_to_list(self._data[field]))

        return recipients

    def build_create(self) -> BaseActivity:
        """Wraps an activity in a Create activity."""
        create_payload = {
            "object": self.to_dict(embed=True),
            "actor": self.attributedTo,
        }
        for field in ["published", "to", "bto", "cc", "bcc", "audience"]:
            if field in self._data:
                create_payload[field] = self._data[field]

        create = Create(**create_payload)
        create.set_ctx(self)

        return create

    def build_like(self) -> BaseActivity:
        return Like(object=self.id)

    def build_announce(self) -> BaseActivity:
        return Announce(
            object=self.id,
            to=[AS_PUBLIC],
            cc=[
                self.follower_collection_id(self.get_actor()),
                self.attributedTo,
            ],  # ABC
            published=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        )

    def build_delete(self) -> BaseActivity:
        return Delete(
            actor=self.get_actor().id, object=Tombstone(id=self.id).to_dict(embed=True)
        )

    def get_tombstone(self, deleted: Optional[str]) -> BaseActivity:
        return Tombstone(
            id=self.id, published=self.published, deleted=deleted, updated=deleted
        )


class Box(object):
    def __init__(self, actor: Person) -> None:
        self.actor = actor


class Outbox(Box):
    def post(self, activity: BaseActivity) -> None:
        if activity.get_actor().id != self.actor.id:
            raise ValueError(
                f"{activity.get_actor()!r} cannot post into {self.actor!r} outbox"
            )

        if activity.ACTIVITY_TYPE == ActivityType.NOTE:
            activity = activity.build_create()

        activity.post_to_outbox()

    def get(self, activity_iri: str) -> BaseActivity:
        pass

    def collection(self):
        # TODO(tsileo): figure out an API
        pass


class Inbox(Box):
    def post(self, activity: BaseActivity) -> None:
        activity.process_from_inbox(self.actor)
