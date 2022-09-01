#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""Base Zen Store implementation."""
import base64
import json
import os
from abc import abstractmethod
from pathlib import Path, PurePath
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Type, Union
from uuid import UUID

from ml_metadata.proto import metadata_store_pb2
from pydantic import BaseModel

from zenml.config.store_config import StoreConfiguration
from zenml.enums import ExecutionStatus, StackComponentType, StoreType
from zenml.logger import get_logger
from zenml.models import ComponentModel, FlavorModel, StackModel
from zenml.models.code_models import CodeRepositoryModel
from zenml.models.pipeline_models import (
    PipelineModel,
    PipelineRunModel,
    StepModel,
)
from zenml.models.user_management_models import (
    ProjectModel,
    RoleModel,
    TeamModel,
    UserModel,
)
from zenml.post_execution import ArtifactView
from zenml.utils import io_utils
from zenml.utils.analytics_utils import AnalyticsEvent, track_event

logger = get_logger(__name__)

DEFAULT_USERNAME = "default"
DEFAULT_PROJECT_NAME = "default"
DEFAULT_STACK_NAME = "default"


class BaseZenStore(BaseModel):
    """Base class for accessing and persisting ZenML core objects.

    Attributes:
        config: The configuration of the store.
        track_analytics: Only send analytics if set to `True`.
    """

    config: StoreConfiguration
    track_analytics: bool = True

    TYPE: ClassVar[StoreType]
    CONFIG_TYPE: ClassVar[Type[StoreConfiguration]]

    def __init__(
        self,
        skip_default_registrations: bool = False,
        **kwargs: Any,
    ) -> None:
        """Create and initialize a store.

        Args:
            skip_default_registrations: If `True`, the creation of the default
                stack and user in the store will be skipped.
            **kwargs: Additional keyword arguments to pass to the Pydantic
                constructor.

        Raises:
            RuntimeError: If the store cannot be initialized.
        """
        super().__init__(**kwargs)

        try:
            self._initialize()
        except Exception as e:
            raise RuntimeError(
                f"Error initializing {self.type.value} store with URL "
                f"'{self.url}': {str(e)}"
            ) from e

        if not skip_default_registrations:
            self._initialize_database()

    @staticmethod
    def get_store_class(type: StoreType) -> Type["BaseZenStore"]:
        """Returns the class of the given store type.

        Args:
            type: The type of the store to get the class for.

        Returns:
            The class of the given store type or None if the type is unknown.

        Raises:
            TypeError: If the store type is unsupported.
        """
        from zenml.zen_stores.sql_zen_store import SqlZenStore

        # from zenml.zen_stores.rest_zen_store import RestZenStore

        store_class = {
            StoreType.SQL: SqlZenStore,
            # StoreType.REST: RestZenStore,
        }.get(type)

        if store_class is None:
            raise TypeError(
                f"No store implementation found for store type "
                f"`{type.value}`."
            )

        return store_class

    @staticmethod
    def create_store(
        config: StoreConfiguration,
        skip_default_registrations: bool = False,
        **kwargs: Any,
    ) -> "BaseZenStore":
        """Create and initialize a store from a store configuration.

        Args:
            config: The store configuration to use.
            skip_default_registrations: If `True`, the creation of the default
                stack and user in the store will be skipped.
            **kwargs: Additional keyword arguments to pass to the store class

        Returns:
            The initialized store.
        """
        logger.debug(f"Creating store with config '{config}'...")
        store_class = BaseZenStore.get_store_class(config.type)
        store = store_class(
            config=config,
            skip_default_registrations=skip_default_registrations,
        )
        return store

    @staticmethod
    def get_default_store_config(path: str) -> StoreConfiguration:
        """Get the default store configuration.

        The default store is a SQLite store that saves the DB contents on the
        local filesystem.

        Args:
            path: The local path where the store DB will be stored.

        Returns:
            The default store configuration.
        """
        from zenml.zen_stores.sql_zen_store import (
            SqlZenStore,
            SqlZenStoreConfiguration,
        )

        config = SqlZenStoreConfiguration(
            type=StoreType.SQL, url=SqlZenStore.get_local_url(path)
        )
        return config

    def _initialize_database(self) -> None:
        """Initialize the database on first use."""
        self.create_default_user()
        self.create_default_project()
        if self.stacks_empty:
            logger.info("Initializing database...")
            self.register_default_stack()

    @property
    def default_user_id(self) -> UUID:
        """Get the ID of the default user, or None if it doesn't exist."""
        try:
            return self.get_user(DEFAULT_USERNAME).id
        except KeyError:
            return None

    @property
    def default_project_id(self) -> str:
        """Get the ID of the default project, or None if it doesn't exist."""
        try:
            return self.get_project(DEFAULT_PROJECT_NAME).id
        except KeyError:
            return None

    def create_default_user(self) -> None:
        """Creates a default user."""
        if not self.default_user_id:
            self._track_event(AnalyticsEvent.CREATED_DEFAULT_USER)
            self._create_user(UserModel(name=DEFAULT_USERNAME))

    def create_default_project(self) -> None:
        """Creates a default project."""
        if not self.default_project_id:
            self._track_event(AnalyticsEvent.CREATED_DEFAULT_PROJECT)
            self._create_project(ProjectModel(name=DEFAULT_PROJECT_NAME))

    def register_default_stack(self) -> None:
        """Populates the store with the default Stack.

        The default stack contains a local orchestrator and a local artifact
        store.
        """
        from zenml.config.global_config import GlobalConfiguration

        orchestrator_config = {}
        encoded_orchestrator_config = base64.urlsafe_b64encode(
            json.dumps(orchestrator_config).encode()
        )
        # Register the default orchestrator
        # try:
        orchestrator = self.register_stack_component(
            user_id=self.default_user_id,
            project_id=self.default_project_id,
            component=ComponentModel(
                name="default",
                type=StackComponentType.ORCHESTRATOR,
                flavor_name="default",
                configuration=encoded_orchestrator_config,
            ),
        )
        # except StackComponentExistsError:
        #     logger.warning("Default Orchestrator exists already, "
        #                    "skipping creation ...")

        # Register the default artifact store
        artifact_store_path = os.path.join(
            GlobalConfiguration().config_directory,
            "local_stores",
            "default_local_store",
        )
        io_utils.create_dir_recursive_if_not_exists(artifact_store_path)
        artifact_store_config = {"path": artifact_store_path}
        encoded_artifact_store_config = base64.urlsafe_b64encode(
            json.dumps(artifact_store_config).encode()
        )
        # try:
        artifact_store = self.register_stack_component(
            user_id=self.default_user_id,
            project_id=self.default_project_id,
            component=ComponentModel(
                name="default",
                type=StackComponentType.ARTIFACT_STORE,
                flavor_name="default",
                configuration=encoded_artifact_store_config,
            ),
        )
        # except StackComponentExistsError:
        #     logger.warning("Default Artifact Store exists already, "
        #                    "skipping creation ...")

        components = {c.type: c for c in [orchestrator, artifact_store]}
        # Register the default stack
        stack = StackModel(
            name="default", components=components, is_shared=True
        )
        self._register_stack(
            user_id=self.default_user_id,
            project_id=self.default_project_id,
            stack=stack,
        )
        self._track_event(
            AnalyticsEvent.REGISTERED_DEFAULT_STACK,
        )

    @property
    def stacks(self) -> List[StackModel]:
        """All stacks registered in this zen store.

        Returns:
            A list of all stacks registered in this zen store.
        """
        return self.list_stacks(project_id=self.default_project_id)

    @property
    def url(self) -> str:
        """The URL of the store.

        Returns:
            The URL of the store.
        """
        return self.config.url

    @property
    def type(self) -> StoreType:
        """The type of the store.

        Returns:
            The type of the store.
        """
        return self.TYPE

    # Static methods:

    @staticmethod
    @abstractmethod
    def get_path_from_url(url: str) -> Optional[Path]:
        """Get the path from a URL, if it points or is backed by a local file.

        Args:
            url: The URL to get the path from.

        Returns:
            The local path backed by the URL, or None if the URL is not backed
            by a local file or directory
        """

    @staticmethod
    @abstractmethod
    def get_local_url(path: str) -> str:
        """Get a local URL for a given local path.

        Args:
            path: the path string to build a URL out of.

        Returns:
            Url pointing to the path for the store type.
        """

    @staticmethod
    @abstractmethod
    def validate_url(url: str) -> str:
        """Check if the given url is valid.

        The implementation should raise a ValueError if the url is invalid.

        Args:
            url: The url to check.

        Returns:
            The modified url, if it is valid.
        """

    @classmethod
    @abstractmethod
    def copy_local_store(
        cls,
        config: StoreConfiguration,
        path: str,
        load_config_path: Optional[PurePath] = None,
    ) -> StoreConfiguration:
        """Copy a local store to a new location.

        Use this method to create a copy of a store database to a new location
        and return a new store configuration pointing to the database copy. This
        only applies to stores that use the local filesystem to store their
        data. Calling this method for remote stores simply returns the input
        store configuration unaltered.

        Args:
            config: The configuration of the store to copy.
            path: The new local path where the store DB will be copied.
            load_config_path: path that will be used to load the copied store
                database. This can be set to a value different from `path`
                if the local database copy will be loaded from a different
                environment, e.g. when the database is copied to a container
                image and loaded using a different absolute path. This will be
                reflected in the paths and URLs encoded in the copied store
                configuration.

        Returns:
            The store configuration of the copied store.
        """

    # Public Interface:

    # .--------.
    # | STACKS |
    # '--------'

    @property
    @abstractmethod
    def stacks_empty(self) -> bool:
        """Check if the store is empty (no stacks are configured).

        The implementation of this method should check if the store is empty
        without having to load all the stacks from the persistent storage.

        Returns:
            True if the store is empty, False otherwise.
        """

    # TODO: [ALEX] add filtering param(s)
    def list_stacks(
        self,
        project_id: str,
        user_id: Optional[UUID] = None,
        name: Optional[str] = None,
        is_shared: Optional[bool] = None,
    ) -> List[StackModel]:
        """List all stacks within the filter.

        Args:
            project_id: Id of the Project containing the stack components
            user_id: Optionally filter stack components by the owner
            name: Optionally filter stack component by name
            is_shared: Optionally filter out stack component by the `is_shared`
                       flag
        Returns:
            A list of all stacks.
        """
        return self._list_stacks(project_id, user_id, name, is_shared)

    @abstractmethod
    def _list_stacks(
        self,
        project_id: str,
        owner: Optional[UUID] = None,
        name: Optional[str] = None,
        is_shared: Optional[bool] = None,
    ) -> List[StackModel]:
        """List all stacks within the filter.

        Args:
            project_id: Id of the Project containing the stack components
            user_id: Optionally filter stack components by the owner
            name: Optionally filter stack component by name
            is_shared: Optionally filter out stack component by the `is_shared`
                       flag
        Returns:
            A list of all stacks.
        """

    @abstractmethod
    def _initialize(self) -> None:
        """Initialize the store.

        This method is called immediately after the store is created. It should
        be used to set up the backend (database, connection etc.).
        """

    def get_stack(self, stack_id: UUID) -> StackModel:
        """Get a stack by id.

        Args:
            stack_id: The id of the stack to get.

        Returns:
            The stack with the given id.

        Raises:
            KeyError: if the stack doesn't exist.
        """
        return self._get_stack(stack_id)

    @abstractmethod
    def _get_stack(self, stack_id: UUID) -> StackModel:
        """Get a stack by ID.

        Args:
            stack_id: The ID of the stack to get.

        Returns:
            The stack.

        Raises:
            KeyError: if the stack doesn't exist.
        """

    def register_stack(
        self, user_id: UUID, project_id: str, stack: StackModel
    ) -> StackModel:
        """Register a new stack.

        Args:
            stack: The stack to register.
            user_id: The user that is registering this stack
            project_id: The project within which that stack is registered

        Returns:
            The registered stack.

        Raises:
            StackExistsError: In case a stack with that name is already owned
                by this user on this project.
        """
        metadata = {c.type.value: c.flavor for c in stack.components}
        metadata["store_type"] = self.type.value
        self._track_event(AnalyticsEvent.REGISTERED_STACK, metadata=metadata)
        return self._register_stack(
            stack=stack, user_id=user_id, project_id=project_id
        )

    @abstractmethod
    def _register_stack(
        self, user_id: UUID, project_id: str, stack: StackModel
    ) -> StackModel:
        """Register a new stack.

        Args:
            stack: The stack to register.
            user_id: The user that is registering this stack
            project_id: The project within which that stack is registered

        Returns:
            The registered stack.

        Raises:
            StackExistsError: In case a stack with that name is already owned
                by this user on this project.
        """

    # def _register_stack_and_stack_components(
    #     self,
    #     user_id: str,
    #     project_id: str,
    #     stack: StackModel
    # ) -> StackModel:
    #     """Register a new stack and all of its components.

    #     Args:
    #         stack: The stack to register.
    #         user_id: The user that is registering this stack
    #         project_id: The project within which that stack is registered

    #     Returns:
    #         The registered stack.

    #     Raises:
    #         StackExistsError: In case a stack with that name is already owned
    #             by this user on this project.
    #     """
    #     for component in stack.components:
    #         try:
    #             self._register_stack_component(
    #                 user_id=user_id,
    #                 project_id=project_id,
    #                 component=component
    #             )
    #         except StackComponentExistsError:
    #             pass
    #     return self._register_stack(
    #         user_id=user_id,
    #         project_id=project_id,
    #         stack=stack
    #     )

    def update_stack(
        self,
        stack_id: str,
        user: UserModel,
        project: ProjectModel,
        stack: StackModel,
    ) -> StackModel:
        """Update an existing stack.

        Args:
            stack_id: The id of the stack to update.
            stack: The stack to update.

        Returns:
            The updated stack.
        """
        metadata = {c.type.value: c.flavor for c in stack.components}
        metadata["store_type"] = self.type.value
        track_event(AnalyticsEvent.UPDATED_STACK, metadata=metadata)
        return self._update_stack(
            stack_id=stack_id, user=user, project=project, stack=stack
        )

    @abstractmethod
    def _update_stack(
        self,
        stack_id: str,
        user: UserModel,
        project: ProjectModel,
        stack: StackModel,
    ) -> StackModel:
        """Update a stack.

        Args:
            stack_id: The ID of the stack to update.
            stack: The stack to use for the update.

        Returns:
            The updated stack.

        Raises:
            KeyError: if the stack doesn't exist.
        """

    def delete_stack(self, stack_id: UUID) -> None:
        """Delete a stack.

        Args:
            stack_id: The id of the stack to delete.
        """
        # No tracking events, here for consistency
        self._delete_stack(stack_id)

    @abstractmethod
    def _delete_stack(self, stack_id: UUID) -> None:
        """Delete a stack.

        Args:
            stack_id: The ID of the stack to delete.

        Raises:
            KeyError: if the stack doesn't exist.
        """

    #  .-----------------.
    # | STACK COMPONENTS |
    # '------------------'

    # TODO: [ALEX] add filtering param(s)
    def list_stack_components(
        self,
        project_id: UUID,
        type: Optional[str] = None,
        flavor_name: Optional[str] = None,
        user_id: Optional[str] = None,
        name: Optional[str] = None,
        is_shared: Optional[bool] = None,
    ) -> List[ComponentModel]:
        """List all stack components within the filter.

        Args:
            project_id: Id of the Project containing the stack components
            type: Optionally filter by type of stack component
            flavor_name: Optionally filter by flavor
            user_id: Optionally filter stack components by the owner
            name: Optionally filter stack component by name
            is_shared: Optionally filter out stack component by the `is_shared`
                       flag

        Returns:
            All stack components currently registered.
        """
        return self._list_stack_components(project_id=project_id,
                                           type=type,
                                           flavor_name=flavor_name,
                                           user_id=user_id,
                                           name=name,
                                           is_shared=is_shared)

    @abstractmethod
    def _list_stack_components(
        self,
        project_id: UUID,
        type: Optional[str] = None,
        flavor_name: Optional[str] = None,
        user_id: Optional[str] = None,
        name: Optional[str] = None,
        is_shared: Optional[bool] = None,
    ) -> List[ComponentModel]:
        """List all stack components within the filter.

        Args:
            project_id: Id of the Project containing the stack components
            type: Optionally filter by type of stack component
            flavor_name: Optionally filter by flavor
            owner: Optionally filter stack components by the owner
            name: Optionally filter stack component by name
            is_shared: Optionally filter out stack component by the `is_shared`
                       flag

        Returns:
            A list of all stack components.
        """

    def get_stack_component(self, component_id: UUID) -> ComponentModel:
        """Get a stack component by id.

        Args:
            component_id: The id of the stack component to get.

        Returns:
            The stack component with the given id.
        """
        return self._get_stack_component(component_id)

    @abstractmethod
    def _get_stack_component(self, component_id: UUID) -> ComponentModel:
        """Get a stack component by ID.

        Args:
            component_id: The ID of the stack component to get.

        Returns:
            The stack component.

        Raises:
            KeyError: if the stack component doesn't exist.
        """

    def register_stack_component(
        self, user_id: UUID, project_id: str, component: ComponentModel
    ) -> ComponentModel:
        """Create a stack component.

        Args:
            user_id: The user that created the stack component.
            project_id: The project the stack component is created in.
            component: The stack component to create.

        Returns:
            The created stack component.
        """
        return self._register_stack_component(
            user_id=user_id, project_id=project_id, component=component
        )

    @abstractmethod
    def _register_stack_component(
        self, user_id: UUID, project_id: str, component: ComponentModel
    ) -> ComponentModel:
        """Create a stack component.

        Args:
            user_id: The user that created the stack component.
            project_id: The project the stack component is created in.
            component: The stack component to create.

        Returns:
            The created stack component.
        """

    def update_stack_component(
        self,
        user_id: str,
        project_id: str,
        component_id: str,
        component: ComponentModel,
    ) -> ComponentModel:
        """Update an existing stack component.

        Args:
            user_id: The user that created the stack component.
            project_id: The project the stack component is created in.
            component_id: The id of the stack component to update.
            component: The stack component to use for the update.

        Returns:
            The updated stack component.
        """
        analytics_metadata = {
            "type": component.type.value,
            "flavor": component.flavor,
        }
        self._track_event(
            AnalyticsEvent.UPDATED_STACK_COMPONENT,
            metadata=analytics_metadata,
        )
        return self._update_stack_component(
            user_id, project_id, component_id, component
        )

    @abstractmethod
    def _update_stack_component(
        self,
        user_id: str,
        project_id: str,
        component_id: str,
        component: ComponentModel,
    ) -> ComponentModel:
        """Update an existing stack component.

        Args:
            user_id: The user that created the stack component.
            project_id: The project the stack component is created in.
            component_id: The id of the stack component to update.
            component: The stack component to use for the update.

        Returns:
            The updated stack component.

        Raises:
            KeyError: if the stack component doesn't exist.
        """

    def delete_stack_component(self, component_id: str) -> None:
        """Delete a stack component.

        Args:
            component_id: The id of the stack component to delete.

        Raises:
            KeyError: if the stack component doesn't exist.
        """
        self._delete_stack_component(component_id)

    @abstractmethod
    def _delete_stack_component(self, component_id: str) -> None:
        """Delete a stack component.

        Args:
            component_id: The ID of the stack component to delete.

        Raises:
            KeyError: if the stack component doesn't exist.
        """

    def get_stack_component_side_effects(
        self, component_id: str, run_id: str, pipeline_id: str, stack_id: str
    ) -> Dict[Any, Any]:
        """Get the side effects of a stack component.

        Args:
            component_id: The id of the stack component to get side effects for.
            run_id: The id of the run to get side effects for.
            pipeline_id: The id of the pipeline to get side effects for.
            stack_id: The id of the stack to get side effects for.
        """
        return self._get_stack_component_side_effects(
            component_id, run_id, pipeline_id, stack_id
        )

    @abstractmethod
    def _get_stack_component_side_effects(
        self, component_id: str, run_id: str, pipeline_id: str, stack_id: str
    ) -> Dict[Any, Any]:
        """Get the side effects of a stack component.

        Args:
            component_id: The ID of the stack component to get side effects for.
            run_id: The ID of the run to get side effects for.
            pipeline_id: The ID of the pipeline to get side effects for.
            stack_id: The ID of the stack to get side effects for.
        """

    def list_stack_component_types(self) -> List[StackComponentType]:
        """List all stack component types.

        Returns:
            A list of all stack component types.
        """
        return self._list_stack_component_types()

    @abstractmethod
    def _list_stack_component_types(self) -> List[StackComponentType]:
        """List all stack component types.

        Returns:
            A list of all stack component types.
        """

    def list_stack_component_flavors_by_type(
        self,
        component_type: StackComponentType,
    ) -> List[FlavorModel]:
        """List all stack component flavors by type.

        Args:
            component_type: The stack component for which to get flavors.

        Returns:
            List of stack component flavors.
        """
        return self._list_stack_component_flavors_by_type(component_type)

    @abstractmethod
    def _list_stack_component_flavors_by_type(
        self, component_type: StackComponentType
    ) -> List[FlavorModel]:
        """List all stack component flavors by type.

        Args:
            component_type: The stack component for which to get flavors.

        Returns:
            List of stack component flavors.
        """

    #  .------.
    # | USERS |
    # '-------'

    @property
    def active_user(self) -> UserModel:
        """The active user.

        Returns:
            The active user.
        """
        return self.get_user(self.active_user_name)

    @property
    @abstractmethod
    def active_user_name(self) -> str:
        """Gets the active username.

        Returns:
            The active username.
        """

    @property
    def users(self) -> List[UserModel]:
        """All registered users.

        Returns:
            A list of all registered users.
        """
        return self.list_users()

    # TODO: make the invite_token optional
    # TODO: [ALEX] add filtering param(s)
    def list_users(self, invite_token: str = None) -> List[UserModel]:
        """List all users.

        Args:
            invite_token: Token to use for the invitation.

        Returns:
            A list of all users.
        """
        return self._list_users(invite_token=invite_token)

    @abstractmethod
    def _list_users(self, invite_token: str = None) -> List[UserModel]:
        """List all users.

        Args:
            invite_token: The invite token to filter by.

        Returns:
            A list of all users.
        """

    def create_user(self, user: UserModel) -> UserModel:
        """Creates a new user.

        Args:
            user: The user model to create.

        Returns:
            The newly created user.

        Raises:
            EntityExistsError: If a user with the given name already exists.
        """
        self._track_event(AnalyticsEvent.CREATED_USER)
        return self._create_user(user)

    @abstractmethod
    def _create_user(self, user: UserModel) -> UserModel:
        """Creates a new user.

        Args:
            user: The user model to create.

        Returns:
            The newly created user.

        Raises:
            EntityExistsError: If a user with the given name already exists.
        """

    def get_user(
        self, user_name_or_id: str, invite_token: str = None
    ) -> UserModel:
        """Gets a specific user.

        Args:
            user_name_or_id: The name or ID of the user to get.
            invite_token: Token to use for the invitation.

        Returns:
            The requested user, if it was found.

        Raises:
            KeyError: If no user with the given name or ID exists.
        """
        # No tracking events, here for consistency
        return self._get_user(
            user_name_or_id=user_name_or_id, invite_token=invite_token
        )

    @abstractmethod
    def _get_user(
        self, user_name_or_id: str, invite_token: str = None
    ) -> UserModel:
        """Gets a specific user.

        Args:
            user_name_or_id: The name or ID of the user to get.
            invite_token: Token to use for the invitation.

        Returns:
            The requested user, if it was found.

        Raises:
            KeyError: If no user with the given name or ID exists.
        """

    def update_user(self, user_id: str, user: UserModel) -> UserModel:
        """Updates an existing user.

        Args:
            user_id: The ID of the user to update.
            user: The user model to use for the update.

        Returns:
            The updated user.

        Raises:
            KeyError: If no user with the given name exists.
        """
        # No tracking events, here for consistency
        return self._update_user(user_id, user)

    @abstractmethod
    def _update_user(self, user_id: str, user: UserModel) -> UserModel:
        """Update the user.

        Args:
            user_id: The ID of the user to update.
            user: The user model to use for the update.

        Returns:
            The updated user.

        Raises:
            KeyError: If no user with the given name exists.
        """

    def delete_user(self, user_id: str) -> None:
        """Deletes a user.

        Args:
            user_id: The ID of the user to delete.

        Raises:
            KeyError: If no user with the given ID exists.
        """
        self._track_event(AnalyticsEvent.DELETED_USER)
        return self._delete_user(user_id)

    @abstractmethod
    def _delete_user(self, user_id: str) -> None:
        """Deletes a user.

        Args:
            user_id: The ID of the user to delete.

        Raises:
            KeyError: If no user with the given ID exists.
        """

    # TODO: Check whether this needs to be an abstract method or not (probably?)
    @abstractmethod
    def get_invite_token(self, user_id: str) -> str:
        """Gets an invite token for a user.

        Args:
            user_id: ID of the user.

        Returns:
            The invite token for the specific user.
        """

    @abstractmethod
    def invalidate_invite_token(self, user_id: str) -> None:
        """Invalidates an invite token for a user.

        Args:
            user_id: ID of the user.
        """

    #  .------.
    # | TEAMS |
    # '-------'

    @property
    def teams(self) -> List[TeamModel]:
        """List all teams.

        Returns:
            A list of all teams.
        """
        return self._list_teams()

    @abstractmethod
    def _list_teams(self) -> List[TeamModel]:
        """List all teams.

        Returns:
            A list of all teams.
        """

    def create_team(self, team: TeamModel) -> TeamModel:
        """Creates a new team.

        Args:
            team: The team model to create.

        Returns:
            The newly created team.
        """
        self._track_event(AnalyticsEvent.CREATED_TEAM)
        return self._create_team(team)

    @abstractmethod
    def _create_team(self, team: TeamModel) -> TeamModel:
        """Creates a new team.

        Args:
            team: The team model to create.

        Returns:
            The newly created team.

        Raises:
            EntityExistsError: If a team with the given name already exists.
        """

    def get_team(self, team_name_or_id: str) -> TeamModel:
        """Gets a specific team.

        Args:
            team_name_or_id: Name or ID of the team to get.

        Returns:
            The requested team.

        Raises:
            KeyError: If no team with the given name or ID exists.
        """
        # No tracking events, here for consistency
        return self._get_team(team_name_or_id)

    @abstractmethod
    def _get_team(self, team_name_or_id: str) -> TeamModel:
        """Gets a specific team.

        Args:
            team_name_or_id: Name or ID of the team to get.

        Returns:
            The requested team.

        Raises:
            KeyError: If no team with the given name or ID exists.
        """

    def delete_team(self, team_id: str) -> None:
        """Deletes a team.

        Args:
            team_id: ID of the team to delete.

        Raises:
            KeyError: If no team with the given ID exists.
        """
        self._track_event(AnalyticsEvent.DELETED_TEAM)
        return self._delete_team(team_id)

    @abstractmethod
    def _delete_team(self, team_id: str) -> None:
        """Deletes a team.

        Args:
            team_id: ID of the team to delete.

        Raises:
            KeyError: If no team with the given ID exists.
        """

    @abstractmethod
    def add_user_to_team(self, user_id: str, team_id: str) -> None:
        """Adds a user to a team.

        Args:
            user_id: ID of the user to add to the team.
            team_id: ID of the team to which to add the user to.

        Raises:
            KeyError: If the team or user does not exist.
        """

    @abstractmethod
    def remove_user_from_team(self, user_id: str, team_id: str) -> None:
        """Removes a user from a team.

        Args:
            user_id: ID of the user to remove from the team.
            team_id: ID of the team from which to remove the user.

        Raises:
            KeyError: If the team or user does not exist.
        """

    @abstractmethod
    def get_users_for_team(self, team_id: str) -> List[UserModel]:
        """Fetches all users of a team.

        Args:
            team_id: The ID of the team for which to get users.

        Returns:
            A list of all users that are part of the team.

        Raises:
            KeyError: If no team with the given ID exists.
        """

    @abstractmethod
    def get_teams_for_user(self, user_id: str) -> List[TeamModel]:
        """Fetches all teams for a user.

        Args:
            user_id: The ID of the user for which to get all teams.

        Returns:
            A list of all teams that the user is part of.

        Raises:
            KeyError: If no user with the given ID exists.
        """

    #  .------.
    # | ROLES |
    # '-------'

    # TODO: create & delete roles?

    @property
    def roles(self) -> List[RoleModel]:
        """All registered roles.

        Returns:
            A list of all registered roles.
        """
        return self.list_roles()

    # TODO: [ALEX] add filtering param(s)
    @abstractmethod
    def list_roles(self) -> List[RoleModel]:
        """List all roles.

        Returns:
            A list of all roles.
        """

    def get_role_assignments_for_user(
        self,
        user_id: str,
        # include_team_roles: bool = True, # TODO: Remove these from the
        # SQLStore implementation
    ) -> List[RoleModel]:
        """Fetches all role assignments for a user.

        Args:
            user_id: ID of the user.

        Returns:
            List of role assignments for this user.

        Raises:
            KeyError: If no user or project with the given names exists.
        """
        return self._get_role_assignments_for_user(user_id)

    @abstractmethod
    def _get_role_assignments_for_user(self, user_id: str) -> List[RoleModel]:
        """Fetches all role assignments for a user.

        Args:
            user_id: ID of the user.

        Returns:
            List of role assignments for this user.

        Raises:
            KeyError: If no user or project with the given names exists.
        """

    def assign_role(self, user_id: str, role_id: str, project_id: str) -> None:
        """Assigns a role to a user or team, scoped to a specific project.

        Args:
            user_id: ID of the user.
            role_id: ID of the role to assign to the user.
            project_id: ID of the project in which to assign the role to the
                user.

        Raises:
            KeyError: If no user, role, or project with the given IDs exists.
        """
        return self._assign_role(user_id, role_id, project_id)

    @abstractmethod
    def _assign_role(self, user_id: str, role: RoleModel) -> None:
        """Assigns a role to a user or team, scoped to a specific project.

        Args:
            user_id: ID of the user.
            role_id: ID of the role to assign to the user.
            project_id: ID of the project in which to assign the role to the
                user.

        Raises:
            KeyError: If no user, role, or project with the given IDs exists.
        """

    def unassign_role(
        self, user_id: str, role_id: str, project_id: str
    ) -> None:
        """Unassigns a role from a user or team for a given project.

        Args:
            user_id: ID of the user.
            role_id: ID of the role to unassign.
            project_id: ID of the project in which to unassign the role from the
                user.

        Raises:
            KeyError: If the role was not assigned to the user in the given
                project.
        """
        self._track_event(AnalyticsEvent.DELETED_ROLE)
        return self._unassign_role(user_id, role_id, project_id)

    @abstractmethod
    def _unassign_role(
        self, user_id: str, role_id: str, project_id: str
    ) -> None:
        """Unassigns a role from a user or team for a given project.

        Args:
            user_id: ID of the user.
            role_id: ID of the role to unassign.
            project_id: ID of the project in which to unassign the role from the
                user.

        Raises:
            KeyError: If the role was not assigned to the user in the given
                project.
        """

    #  .----------------.
    # | METADATA_CONFIG |
    # '-----------------'

    @abstractmethod
    def get_metadata_config(
        self,
    ) -> Union[
        metadata_store_pb2.ConnectionConfig,
        metadata_store_pb2.MetadataStoreClientConfig,
    ]:
        """Get the TFX metadata config of this ZenStore.

        Returns:
            The TFX metadata config of this ZenStore.
        """

    #  .---------.
    # | PROJECTS |
    # '----------'

    @property
    def projects(self) -> List[ProjectModel]:
        """All registered projects.

        Returns:
            A list of all registered projects.
        """
        return self.list_projects()

    # TODO: [ALEX] add filtering param(s)
    def list_projects(self) -> List[ProjectModel]:
        """List all projects.

        Returns:
            A list of all projects.
        """
        return self._list_projects()

    @abstractmethod
    def _list_projects(self) -> List[ProjectModel]:
        """List all projects.

        Returns:
            A list of all projects.
        """

    def create_project(self, project: ProjectModel) -> ProjectModel:
        """Creates a new project.

        Args:
            project: The project to create.

        Returns:
            The newly created project.

        Raises:
            EntityExistsError: If a project with the given name already exists.
        """
        self._track_event(AnalyticsEvent.CREATED_PROJECT)
        return self._create_project(project)

    @abstractmethod
    def _create_project(self, project: ProjectModel) -> ProjectModel:
        """Creates a new project.

        Args:
            project: The project to create.

        Returns:
            The newly created project.

        Raises:
            EntityExistsError: If a project with the given name already exists.
        """

    def get_project(self, project_name_or_id: str) -> ProjectModel:
        """Get an existing project by name or ID.

        Args:
            project_name_or_id: Name or ID of the project to get.

        Returns:
            The requested project if one was found.

        Raises:
            KeyError: If there is no such project.
        """
        # No tracking events, here for consistency
        return self._get_project(project_name_or_id)

    @abstractmethod
    def _get_project(self, project_name_or_id: str) -> ProjectModel:
        """Get an existing project by name or ID.

        Args:
            project_name_or_id: Name or ID of the project to get.

        Returns:
            The requested project if one was found.

        Raises:
            KeyError: If there is no such project.
        """

    def update_project(
        self, project_name: str, project: ProjectModel
    ) -> ProjectModel:
        """Updates an existing project.

        Args:
            project_name: Name of the project to update.
            project: The project to use for the update.

        Returns:
            The updated project.

        Raises:
            KeyError: if the project does not exist.
        """
        # No tracking events, here for consistency
        return self._update_project(project_name, project)

    @abstractmethod
    def _update_project(
        self, project_name: str, project: ProjectModel
    ) -> ProjectModel:
        """Update an existing project.

        Args:
            project_name: Name of the project to update.
            project: The project to use for the update.

        Returns:
            The updated project.

        Raises:
            KeyError: if the project does not exist.
        """

    def delete_project(self, project_name: str) -> None:
        """Deletes a project.

        Args:
            project_name: Name of the project to delete.

        Raises:
            KeyError: If the project does not exist.
        """
        self._track_event(AnalyticsEvent.DELETED_PROJECT)
        return self._delete_project(project_name)

    @abstractmethod
    def _delete_project(self, project_name: str) -> None:
        """Deletes a project.

        Args:
            project_name: Name of the project to delete.

        Raises:
            KeyError: If no project with the given name exists.
        """

    def get_default_stack(self, project_name: str) -> StackModel:
        """Gets the default stack in a project.

        Args:
            project_name: Name of the project to get.

        Returns:
            The default stack in the project.

        Raises:
            KeyError: if the project doesn't exist.
        """
        return self._get_default_stack(project_name)

    @abstractmethod
    def _get_default_stack(self, project_name: str) -> StackModel:
        """Gets the default stack in a project.

        Args:
            project_name: Name of the project to get.

        Returns:
            The default stack in the project.

        Raises:
            KeyError: if the project doesn't exist.
        """

    def set_default_stack(self, project_name: str, stack_id: str) -> StackModel:
        """Sets the default stack in a project.

        Args:
            project_name: Name of the project to set.
            stack_id: The ID of the stack to set as the default.

        Raises:
            KeyError: if the project or stack doesn't exist.
        """
        return self._set_default_stack(project_name, stack_id)

    @abstractmethod
    def _set_default_stack(
        self, project_name: str, stack_id: str
    ) -> StackModel:
        """Sets the default stack in a project.

        Args:
            project_name: Name of the project to set.
            stack_id: The ID of the stack to set as the default.

        Raises:
            KeyError: if the project or stack doesn't exist.
        """

    #  .-------------.
    # | REPOSITORIES |
    # '--------------'

    # TODO: create repository?

    # TODO: [ALEX] add filtering param(s)
    def list_project_repositories(
        self, project_name: str
    ) -> List[CodeRepositoryModel]:
        """Gets all repositories in a project.

        Args:
            project_name: The name of the project.

        Returns:
            A list of all repositories in the project.

        Raises:
            KeyError: if the project doesn't exist.
        """
        return self._list_project_repositories(project_name)

    @abstractmethod
    def _list_project_repositories(
        self, project_name: str
    ) -> List[CodeRepositoryModel]:
        """Get all repositories in the project.

        Args:
            project_name: The name of the project.

        Returns:
            A list of all repositories in the project.

        Raises:
            KeyError: if the project doesn't exist.
        """

    def connect_project_repository(
        self, project_name: str, repository: CodeRepositoryModel
    ) -> CodeRepositoryModel:
        """Connects a repository to a project.

        Args:
            project_name: Name of the project to connect the repository to.
            repository: The repository to connect.

        Returns:
            The connected repository.

        Raises:
            KeyError: if the project or repository doesn't exist.
        """
        self._track_event(AnalyticsEvent.CONNECT_REPOSITORY)
        return self._connect_project_repository(project_name, repository)

    @abstractmethod
    def _connect_project_repository(
        self, project_name: str, repository: CodeRepositoryModel
    ) -> CodeRepositoryModel:
        """Connects a repository to a project.

        Args:
            project_name: Name of the project to connect the repository to.
            repository: The repository to connect.

        Returns:
            The connected repository.

        Raises:
            KeyError: if the project or repository doesn't exist.
        """

    def get_repository(self, repository_id: str) -> CodeRepositoryModel:
        """Gets a repository.

        Args:
            repository_id: The ID of the repository to get.

        Returns:
            The repository.

        Raises:
            KeyError: if the repository doesn't exist.
        """
        return self._get_repository(repository_id)

    @abstractmethod
    def _get_repository(self, repository_id: str) -> CodeRepositoryModel:
        """Get a repository by ID.

        Args:
            repository_id: The ID of the repository to get.

        Returns:
            The repository.

        Raises:
            KeyError: if the repository doesn't exist.
        """

    def update_repository(
        self, repository_id: str, repository: CodeRepositoryModel
    ):
        """Updates a repository.

        Args:
            repository_id: The ID of the repository to update.
            repository: The repository to use for the update.

        Returns:
            The updated repository.

        Raises:
            KeyError: if the repository doesn't exist.
        """
        self._track_event(AnalyticsEvent.UPDATE_REPOSITORY)
        return self._update_repository(repository_id, repository)

    @abstractmethod
    def _update_repository(
        self, repository_id: str, repository: CodeRepositoryModel
    ) -> CodeRepositoryModel:
        """Update a repository.

        Args:
            repository_id: The ID of the repository to update.
            repository: The repository to use for the update.

        Returns:
            The updated repository.

        Raises:
            KeyError: if the repository doesn't exist.
        """

    def delete_repository(self, repository_id: str):
        """Deletes a repository.

        Args:
            repository_id: The ID of the repository to delete.

        Raises:
            KeyError: if the repository doesn't exist.
        """
        self._track_event(AnalyticsEvent.DELETE_REPOSITORY)
        return self._delete_repository(repository_id)

    @abstractmethod
    def _delete_repository(self, repository_id: str) -> None:
        """Delete a repository.

        Args:
            repository_id: The ID of the repository to delete.

        Raises:
            KeyError: if the repository doesn't exist.
        """

    #  .-----.
    # | AUTH |
    # '------'

    def login(self) -> None:
        """Logs in to the server."""
        self._track_event(AnalyticsEvent.LOGIN)
        self._login()

    def _login(self) -> None:
        """Logs in to the server."""

    def logout(self) -> None:
        """Logs out of the server."""
        self._track_event(AnalyticsEvent.LOGOUT)
        self._logout()

    def _logout(self) -> None:
        """Logs out of the server."""

    #  .----------.
    # | PIPELINES |
    # '-----------'

    # TODO: [ALEX] add filtering param(s)
    def list_pipelines(self, project_name: str) -> List[PipelineModel]:
        """Gets all pipelines in a project.

        Args:
            project_name: Name of the project to get.

        Returns:
            A list of all pipelines in the project.

        Raises:
            KeyError: if the project doesn't exist.
        """
        return self._list_pipelines(project_name)

    @abstractmethod
    def _list_pipelines(self, project_name: str) -> List[PipelineModel]:
        """List all pipelines in the project.

        Args:
            project_name: Name of the project.

        Returns:
            A list of pipelines.

        Raises:
            KeyError: if the project does not exist.
        """

    def create_pipeline(
        self, project_name: str, pipeline: PipelineModel
    ) -> PipelineModel:
        """Creates a new pipeline in a project.

        Args:
            project_name: Name of the project to create the pipeline in.
            pipeline: The pipeline to create.

        Returns:
            The newly created pipeline.

        Raises:
            KeyError: if the project does not exist.
            EntityExistsError: If an identical pipeline already exists.
        """
        self._track_event(AnalyticsEvent.CREATE_PIPELINE)
        return self._create_pipeline(project_name, pipeline)

    @abstractmethod
    def _create_pipeline(
        self, project_name: str, pipeline: PipelineModel
    ) -> PipelineModel:
        """Creates a new pipeline in a project.

        Args:
            project_name: Name of the project to create the pipeline in.
            pipeline: The pipeline to create.

        Returns:
            The newly created pipeline.

        Raises:
            KeyError: if the project does not exist.
            EntityExistsError: If an identical pipeline already exists.
        """

    @abstractmethod
    def get_pipeline(self, pipeline_id: str) -> Optional[PipelineModel]:
        """Returns a pipeline for the given name.

        Args:
            pipeline_id: ID of the pipeline.

        Returns:
            PipelineModel if found, None otherwise.
        """

    def update_pipeline(
        self, pipeline_id: str, pipeline: PipelineModel
    ) -> PipelineModel:
        """Updates a pipeline.

        Args:
            pipeline_id: The ID of the pipeline to update.
            pipeline: The pipeline to use for the update.

        Returns:
            The updated pipeline.

        Raises:
            KeyError: if the pipeline doesn't exist.
        """
        self._track_event(AnalyticsEvent.UPDATE_PIPELINE)
        return self._update_pipeline(pipeline_id, pipeline)

    @abstractmethod
    def _update_pipeline(
        self, pipeline_id: str, pipeline: PipelineModel
    ) -> PipelineModel:
        """Updates a pipeline.

        Args:
            pipeline_id: The ID of the pipeline to update.
            pipeline: The pipeline to use for the update.

        Returns:
            The updated pipeline.

        Raises:
            KeyError: if the pipeline doesn't exist.
        """

    def delete_pipeline(self, pipeline_id: str) -> None:
        """Deletes a pipeline.

        Args:
            pipeline_id: The ID of the pipeline to delete.

        Raises:
            KeyError: if the pipeline doesn't exist.
        """
        self._track_event(AnalyticsEvent.DELETE_PIPELINE)
        return self._delete_pipeline(pipeline_id)

    @abstractmethod
    def _delete_pipeline(self, pipeline_id: str) -> None:
        """Deletes a pipeline.

        Args:
            pipeline_id: The ID of the pipeline to delete.

        Raises:
            KeyError: if the pipeline doesn't exist.
        """

    def get_pipeline_configuration(self, pipeline_id: str) -> Dict[Any, Any]:
        """Gets the pipeline configuration.

        Args:
            pipeline_id: The ID of the pipeline to get.

        Returns:
            The pipeline configuration.

        Raises:
            KeyError: if the pipeline doesn't exist.
        """
        return self._get_pipeline_configuration(pipeline_id)

    @abstractmethod
    def _get_pipeline_configuration(self, pipeline_id: str) -> Dict[Any, Any]:
        """Gets the pipeline configuration.

        Args:
            pipeline_id: The ID of the pipeline to get.

        Returns:
            The pipeline configuration.

        Raises:
            KeyError: if the pipeline doesn't exist.
        """

    # TODO: change into an abstract method
    # TODO: Note that this doesn't have a corresponding API endpoint (consider adding?)
    # TODO: Discuss whether we even need this, given that the endpoint is on
    # pipeline RUNs
    # TODO: [ALEX] add filtering param(s)
    def list_steps(self, pipeline_id: str) -> List[StepModel]:
        """List all steps for a specific pipeline.

        Args:
            pipeline_id: The id of the pipeline to get steps for.

        Returns:
            A list of all steps for the pipeline.
        """
        return self._list_steps(pipeline_id)

    @abstractmethod
    def _list_steps(self, pipeline_id: str) -> List[StepModel]:
        """List all steps.

        Args:
            pipeline_id: The ID of the pipeline to list steps for.

        Returns:
            A list of all steps.
        """

    #  .-----.
    # | RUNS |
    # '------'

    def get_pipeline_runs(self, pipeline_id: str) -> List[PipelineRunModel]:
        """Gets all pipeline runs in a pipeline.

        Args:
            pipeline_id: The ID of the pipeline to get.

        Returns:
            A list of all pipeline runs in the pipeline.

        Raises:
            KeyError: if the pipeline doesn't exist.
        """
        return self._get_pipeline_runs(pipeline_id)

    @abstractmethod
    def _get_pipeline_runs(self, pipeline_id: str) -> List[PipelineRunModel]:
        """Gets all pipeline runs in a pipeline.

        Args:
            pipeline_id: The ID of the pipeline to get.

        Returns:
            A list of all pipeline runs in the pipeline.

        Raises:
            KeyError: if the pipeline doesn't exist.
        """

    def create_pipeline_run(
        self, pipeline_id: str, pipeline_run: PipelineRunModel
    ) -> PipelineRunModel:
        """Creates a pipeline run.

        Args:
            pipeline_id: The ID of the pipeline to create the run in.
            pipeline_run: The pipeline run to create.

        Returns:
            The created pipeline run.

        Raises:
            KeyError: if the pipeline doesn't exist.
        """
        return self._create_pipeline_run(pipeline_id, pipeline_run)

    @abstractmethod
    def _create_pipeline_run(
        self, pipeline_id: str, pipeline_run: PipelineRunModel
    ) -> PipelineRunModel:
        """Creates a pipeline run.

        Args:
            pipeline_id: The ID of the pipeline to create the run in.
            pipeline_run: The pipeline run to create.

        Returns:
            The created pipeline run.

        Raises:
            KeyError: if the pipeline doesn't exist.
        """

    # TODO: [ALEX] add filtering param(s)
    def list_runs(
        self,
        project_name: Optional[str] = None,
        stack_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
    ) -> List[PipelineRunModel]:
        """Gets all pipeline runs in a project.

        Args:
            project_name: Name of the project to get.
            stack_id: ID of the stack to get.
            pipeline_id: ID of the pipeline to get.
            trigger_id: ID of the trigger to get.

        Returns:
            A list of all pipeline runs in the project.

        Raises:
            KeyError: if the project doesn't exist.
        """
        return self._list_pipeline_runs(
            project_name=project_name,
            stack_id=stack_id,
            pipeline_id=pipeline_id,
            trigger_id=trigger_id,
        )

    # TODO: [ALEX] add filtering param(s)
    @abstractmethod
    def _list_pipeline_runs(
        self,
        project_name: Optional[str] = None,
        stack_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        trigger_id: Optional[str] = None,
    ) -> List[PipelineRunModel]:
        """Gets all pipeline runs in a project.

        Args:
            project_name: Name of the project to get.
            stack_id: ID of the stack to get.
            pipeline_id: ID of the pipeline to get.
            trigger_id: ID of the trigger to get.

        Returns:
            A list of all pipeline runs in the project.

        Raises:
            KeyError: if the project doesn't exist.
        """

    def get_run(self, run_id: str) -> PipelineRunModel:
        """Gets a pipeline run.

        Args:
            run_id: The ID of the pipeline run to get.

        Returns:
            The pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """
        return self._get_run(run_id)

    @abstractmethod
    def _get_run(self, run_id: str) -> PipelineRunModel:
        """Gets a pipeline run.

        Args:
            run_id: The ID of the pipeline run to get.

        Returns:
            The pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """

    def update_run(
        self, run_id: str, run: PipelineRunModel
    ) -> PipelineRunModel:
        """Updates a pipeline run.

        Args:
            run_id: The ID of the pipeline run to update.
            run: The pipeline run to use for the update.

        Returns:
            The updated pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """
        return self._update_run(run_id, run)

    @abstractmethod
    def _update_run(
        self, run_id: str, run: PipelineRunModel
    ) -> PipelineRunModel:
        """Updates a pipeline run.

        Args:
            run_id: The ID of the pipeline run to update.
            run: The pipeline run to use for the update.

        Returns:
            The updated pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """

    def delete_run(self, run_id: str) -> None:
        """Deletes a pipeline run.

        Args:
            run_id: The ID of the pipeline run to delete.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """
        return self._delete_run(run_id)

    @abstractmethod
    def _delete_run(self, run_id: str) -> None:
        """Deletes a pipeline run.

        Args:
            run_id: The ID of the pipeline run to delete.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """

    # TODO: figure out args and output for this
    def get_run_dag(self, run_id: str) -> str:
        """Gets the DAG for a pipeline run.

        Args:
            run_id: The ID of the pipeline run to get.

        Returns:
            The DAG for the pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """
        return self._get_run_dag(run_id)

    # TODO: figure out args and output for this
    @abstractmethod
    def _get_run_dag(self, run_id: str) -> str:
        """Gets the DAG for a pipeline run.

        Args:
            run_id: The ID of the pipeline run to get.

        Returns:
            The DAG for the pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """

    def get_run_runtime_configuration(self, run_id: str) -> Dict:
        """Gets the runtime configuration for a pipeline run.

        Args:
            run_id: The ID of the pipeline run to get.

        Returns:
            The runtime configuration for the pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """
        return self._get_run_runtime_configuration(run_id)

    @abstractmethod
    def _get_run_runtime_configuration(self, run_id: str) -> Dict:
        """Gets the runtime configuration for a pipeline run.

        Args:
            run_id: The ID of the pipeline run to get.

        Returns:
            The runtime configuration for the pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """

    # TODO: Figure out what exactly gets returned from this
    def get_run_component_side_effects(
        self,
        run_id: str,
        component_id: Optional[str] = None,
        component_type: Optional[StackComponentType] = None,
    ) -> Dict:
        """Gets the side effects for a component in a pipeline run.

        Args:
            run_id: The ID of the pipeline run to get.
            component_id: The ID of the component to get.

        Returns:
            The side effects for the component in the pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """
        return self._get_run_component_side_effects(
            run_id, component_id=component_id, component_type=component_type
        )

    @abstractmethod
    def _get_run_component_side_effects(
        self,
        run_id: str,
        component_id: Optional[str] = None,
        component_type: Optional[StackComponentType] = None,
    ) -> Dict:
        """Gets the side effects for a component in a pipeline run.

        Args:
            run_id: The ID of the pipeline run to get.
            component_id: The ID of the component to get.

        Returns:
            The side effects for the component in the pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """

    #  .------.
    # | STEPS |
    # '-------'

    # TODO: [ALEX] add filtering param(s)
    def list_run_steps(self, run_id: str) -> List[StepModel]:
        """Gets all steps in a pipeline run.

        Args:
            run_id: The ID of the pipeline run to get.

        Returns:
            A list of all steps in the pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """
        return self._list_run_steps(run_id)

    # TODO: [ALEX] add filtering param(s)
    @abstractmethod
    def _list_run_steps(self, run_id: str) -> List[StepModel]:
        """Gets all steps in a pipeline run.

        Args:
            run_id: The ID of the pipeline run to get.

        Returns:
            A list of all steps in the pipeline run.

        Raises:
            KeyError: if the pipeline run doesn't exist.
        """

    # TODO: change into an abstract method
    def get_run_step(self, step_id: str) -> StepModel:
        """Get a step by id.

        Args:
            step_id: The id of the step to get.

        Returns:
            The step with the given id.
        """
        return self._get_run_step(step_id)

    @abstractmethod
    def _get_run_step(self, step_id: str) -> StepModel:
        """Get a step by ID.

        Args:
            step_id: The ID of the step to get.

        Returns:
            The step.

        Raises:
            KeyError: if the step doesn't exist.
        """

    # TODO: change into an abstract method
    # TODO: use the correct return value + also amend the endpoint as well
    # TODO: use an ArtifactModel for this instead of ArtifactView, to break the
    #       dependency on Repository
    def get_run_step_outputs(self, step_id: str) -> Dict[str, ArtifactView]:
        """Get a list of outputs for a specific step.

        Args:
            step_id: The id of the step to get outputs for.

        Returns:
            A list of Dicts mapping artifact names to the output artifacts for the step.
        """
        return self._get_run_step_outputs(step_id)

    @abstractmethod
    def _get_run_step_outputs(self, step_id: str) -> Dict[str, ArtifactView]:
        """Get the outputs of a step.

        Args:
            step_id: The ID of the step to get outputs for.

        Returns:
            The outputs of the step.
        """

    # TODO: change into an abstract method
    # TODO: Note that this doesn't have a corresponding API endpoint (consider adding?)
    def get_run_step_inputs(self, step_id: str) -> Dict[str, ArtifactView]:
        """Get a list of inputs for a specific step.

        Args:
            step_id: The id of the step to get inputs for.

        Returns:
            A list of Dicts mapping artifact names to the input artifacts for the step.
        """
        return self._get_run_step_inputs(step_id)

    @abstractmethod
    def _get_run_step_inputs(self, step_id: str) -> Dict[str, ArtifactView]:
        """Get the inputs of a step.

        Args:
            step_id: The ID of the step to get inputs for.

        Returns:
            The inputs of the step.
        """

    # # Public facing APIs
    # # TODO [ENG-894]: Refactor these with the proxy pattern, as noted in
    # #  the [review comment](https://github.com/zenml-io/zenml/pull/589#discussion_r875003334)

    # TODO: consider using team_id instead
    def get_role(self, role_name: str) -> RoleModel:
        """Gets a specific role.

        Args:
            role_name: Name of the role to get.

        Returns:
            The requested role.
        """
        # No tracking events, here for consistency
        return self._get_role(role_name)

    # TODO: consider using team_id instead
    def create_role(self, role_name: str) -> RoleModel:
        """Creates a new role.

        Args:
            role_name: Unique role name.

        Returns:
            The newly created role.
        """
        self._track_event(AnalyticsEvent.CREATED_ROLE)
        return self._create_role(role_name)

    def create_flavor(
        self,
        source: str,
        name: str,
        stack_component_type: StackComponentType,
    ) -> FlavorModel:
        """Creates a new flavor.

        Args:
            source: the source path to the implemented flavor.
            name: the name of the flavor.
            stack_component_type: the corresponding StackComponentType.

        Returns:
            The newly created flavor.
        """
        analytics_metadata = {
            "type": stack_component_type.value,
        }
        self._track_event(
            AnalyticsEvent.CREATED_FLAVOR,
            metadata=analytics_metadata,
        )
        return self._create_flavor(source, name, stack_component_type)

    # LEGACY CODE FROM THE PREVIOUS VERSION OF BASEZENSTORE

    # Private interface (must be implemented, not to be called by user):
    @abstractmethod
    def _get_stack_component_names(
        self, component_type: StackComponentType
    ) -> List[str]:
        """Get names of all registered stack components of a given type.

        Args:
            component_type: The type of the component to list names for.

        Returns:
            A list of names as strings.
        """

    @abstractmethod
    def _delete_stack_component(
        self, component_type: StackComponentType, name: str
    ) -> None:
        """Remove a StackComponent from storage.

        Args:
            component_type: The type of component to delete.
            name: Then name of the component to delete.

        Raises:
            KeyError: If no component exists for given type and name.
        """

    # User, project and role management

    @property
    @abstractmethod
    def role_assignments(self) -> List[RoleModel]:
        """All registered role assignments.

        Returns:
            A list of all registered role assignments.
        """

    @abstractmethod
    def _get_role(self, role_name: str) -> RoleModel:
        """Gets a specific role.

        Args:
            role_name: Name of the role to get.

        Returns:
            The requested role.

        Raises:
            KeyError: If no role with the given name exists.
        """

    @abstractmethod
    def _create_role(self, role_name: str) -> RoleModel:
        """Creates a new role.

        Args:
            role_name: Unique role name.

        Returns:
            The newly created role.

        Raises:
            EntityExistsError: If a role with the given name already exists.
        """

    @abstractmethod
    def revoke_role(
        self,
        role_name: str,
        entity_name: str,
        project_name: Optional[str] = None,
        is_user: bool = True,
    ) -> None:
        """Revokes a role from a user or team.

        Args:
            role_name: Name of the role to revoke.
            entity_name: User or team name.
            project_name: Optional project name.
            is_user: Boolean indicating whether the given `entity_name` refers
                to a user.

        Raises:
            KeyError: If no role, entity or project with the given names exists.
        """

    @abstractmethod
    def get_role_assignments_for_team(
        self,
        team_name: str,
        project_name: Optional[str] = None,
    ) -> List[RoleModel]:
        """Fetches all role assignments for a team.

        Args:
            team_name: Name of the user.
            project_name: Optional filter to only return roles assigned for
                this project.

        Returns:
            List of role assignments for this team.

        Raises:
            KeyError: If no team or project with the given names exists.
        """

    # Pipelines and pipeline runs

    @abstractmethod
    def get_pipeline_run(
        self, pipeline: PipelineModel, run_name: str
    ) -> Optional[PipelineRunModel]:
        """Gets a specific run for the given pipeline.

        Args:
            pipeline: The pipeline for which to get the run.
            run_name: The name of the run to get.

        Returns:
            The pipeline run with the given name.
        """

    # TODO: [ALEX] add filtering param(s)
    # TODO: Consider changing to list_runs...
    @abstractmethod
    def get_pipeline_runs(
        self, pipeline: PipelineModel
    ) -> Dict[str, PipelineRunModel]:
        """Gets all runs for the given pipeline.

        Args:
            pipeline: a Pipeline object for which you want the runs.

        Returns:
            A dictionary of pipeline run names to PipelineRunView.
        """

    @abstractmethod
    def get_pipeline_run_wrapper(
        self,
        pipeline_name: str,
        run_name: str,
        project_name: Optional[str] = None,
    ) -> PipelineRunModel:
        """Gets a pipeline run.

        Args:
            pipeline_name: Name of the pipeline for which to get the run.
            run_name: Name of the pipeline run to get.
            project_name: Optional name of the project from which to get the
                pipeline run.

        Raises:
            KeyError: If no pipeline run (or project) with the given name
                exists.
        """

    @abstractmethod
    def get_pipeline_run_wrappers(
        self, pipeline_name: str, project_name: Optional[str] = None
    ) -> List[PipelineRunModel]:
        """Gets pipeline runs.

        Args:
            pipeline_name: Name of the pipeline for which to get runs.
            project_name: Optional name of the project from which to get the
                pipeline runs.
        """

    @abstractmethod
    def get_pipeline_run_steps(
        self, pipeline_run: PipelineRunModel
    ) -> Dict[str, StepModel]:
        """Gets all steps for the given pipeline run.

        Args:
            pipeline_run: The pipeline run to get the steps for.

        Returns:
            A dictionary of step names to step views.
        """

    @abstractmethod
    def get_step_status(self, step: StepModel) -> ExecutionStatus:
        """Gets the execution status of a single step.

        Args:
            step (StepView): The step to get the status for.

        Returns:
            ExecutionStatus: The status of the step.
        """

    @abstractmethod
    def get_producer_step_from_artifact(self, artifact_id: int) -> StepModel:
        """Returns original StepView from an ArtifactView.

        Args:
            artifact_id: ID of the ArtifactView to be queried.

        Returns:
            Original StepView that produced the artifact.
        """

    @abstractmethod
    def register_pipeline_run(
        self,
        pipeline_run: PipelineRunModel,
    ) -> None:
        """Registers a pipeline run.

        Args:
            pipeline_run: The pipeline run to register.

        Raises:
            EntityExistsError: If a pipeline run with the same name already
                exists.
        """

    # Stack component flavors

    @property
    @abstractmethod
    def flavors(self) -> List[FlavorModel]:
        """All registered flavors.

        Returns:
            A list of all registered flavors.
        """

    @abstractmethod
    def _create_flavor(
        self,
        source: str,
        name: str,
        stack_component_type: StackComponentType,
    ) -> FlavorModel:
        """Creates a new flavor.

        Args:
            source: the source path to the implemented flavor.
            name: the name of the flavor.
            stack_component_type: the corresponding StackComponentType.

        Returns:
            The newly created flavor.

        Raises:
            EntityExistsError: If a flavor with the given name and type
                already exists.
        """

    @abstractmethod
    def get_flavors_by_type(
        self, component_type: StackComponentType
    ) -> List[FlavorModel]:
        """Fetch all flavor defined for a specific stack component type.

        Args:
            component_type: The type of the stack component.

        Returns:
            List of all the flavors for the given stack component type.
        """

    @abstractmethod
    def get_flavor_by_name_and_type(
        self,
        flavor_name: str,
        component_type: StackComponentType,
    ) -> FlavorModel:
        """Fetch a flavor by a given name and type.

        Args:
            flavor_name: The name of the flavor.
            component_type: Optional, the type of the component.

        Returns:
            Flavor instance if it exists

        Raises:
            KeyError: If no flavor exists with the given name and type
                or there are more than one instances
        """

    # Common code (internal implementations, private):

    def _track_event(
        self,
        event: Union[str, AnalyticsEvent],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Track an analytics event.

        Args:
            event: The event to track.
            metadata: Additional metadata to track with the event.

        Returns:
            True if the event was successfully tracked, False otherwise.
        """
        if self.track_analytics:
            return track_event(event, metadata)
        return False

    class Config:
        """Pydantic configuration class."""

        # Validate attributes when assigning them. We need to set this in order
        # to have a mix of mutable and immutable attributes
        validate_assignment = True
        # Ignore extra attributes from configs of previous ZenML versions
        extra = "ignore"
        # all attributes with leading underscore are private and therefore
        # are mutable and not included in serialization
        underscore_attrs_are_private = True
