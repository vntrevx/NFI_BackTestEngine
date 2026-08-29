use serde_json::Value;

use super::super::types::{CallbackDeltaOperation, CallbackObservation, CallbackProgramException};
use super::super::value::{map_key, truthy, ValueFault, Values};
use super::support::{delta, identity, normalize_none, normalize_return, RuntimeKind};
use super::{Flow, Machine};
use crate::domain::{
    callback_value_matches_type as matches_type, canonical_callback_json_sha256,
    CallbackExpression, CallbackFallback, CallbackReturn, CallbackReturnClass,
    CallbackStatement as S, ExecutableCallbackError as Error,
};

impl Machine<'_> {
    pub(super) fn execute(&mut self, statements: &[S]) -> Result<Flow, Error> {
        for statement in statements {
            let (id, predicates) = identity(statement);
            self.current = Some(id.to_owned());
            self.step()?;
            self.predicates.extend(predicates.iter().cloned());
            let flow = self.execute_statement(statement, id, predicates)?;
            if !matches!(flow, Flow::Continue) {
                return Ok(flow);
            }
        }
        Ok(Flow::Continue)
    }

    pub(super) fn resolve_flow(
        &self,
        flow: Flow,
        fallback: &CallbackFallback,
    ) -> Result<
        (
            CallbackReturnClass,
            Option<Value>,
            super::super::types::CallbackProgramTransaction,
            Option<CallbackProgramException>,
        ),
        Error,
    > {
        match flow {
            Flow::Returned(class, value) => Ok((
                class,
                value,
                super::super::types::CallbackProgramTransaction::Committed,
                None,
            )),
            Flow::Continue => Ok((
                CallbackReturnClass::None,
                None,
                super::super::types::CallbackProgramTransaction::Committed,
                None,
            )),
            Flow::Raised(exception) => {
                let (class, value) = self.fallback_value(fallback)?;
                Ok((
                    class,
                    value,
                    super::super::types::CallbackProgramTransaction::Fallback,
                    Some(exception),
                ))
            }
        }
    }

    fn execute_statement(
        &mut self,
        statement: &S,
        id: &str,
        predicates: &[String],
    ) -> Result<Flow, Error> {
        match statement {
            S::Let { name, value, .. } => self.bind_local(name, value),
            S::SetRegister {
                register_id, value, ..
            } => self.set_register(register_id, value, id, predicates),
            S::SetRegisterItem {
                register_id,
                key,
                value,
                ..
            } => self.set_register_item(register_id, key, value, id, predicates),
            S::SetCustomState { key, value, .. } => self.set_custom(key, value, id, predicates),
            S::DeleteCustomState { key, .. } => Ok(self.delete_custom(key, id, predicates)),
            S::If {
                condition,
                then,
                otherwise,
                ..
            } => self.branch(condition, then, otherwise),
            S::ForRange {
                target,
                bounds,
                body,
                ..
            } => self.for_range(target, bounds, body),
            S::Return { result, .. } => {
                let (class, value) = self.return_value(result)?;
                Ok(Flow::Returned(class, value))
            }
            S::RaiseCallback {
                exception_class,
                message,
                ..
            } => self.raise(exception_class, message, id),
            S::EmitObservation {
                channel, payload, ..
            } => self.observe(channel, payload, id, predicates),
        }
    }

    fn bind_local(&mut self, name: &str, value: &CallbackExpression) -> Result<Flow, Error> {
        let value = self.value(value)?;
        self.locals.insert(name.to_owned(), value);
        Ok(Flow::Continue)
    }

    fn set_register(
        &mut self,
        register_id: &str,
        expression: &CallbackExpression,
        id: &str,
        predicates: &[String],
    ) -> Result<Flow, Error> {
        let value = self.value(expression)?;
        let Some(kind) = self.register_types.get(register_id) else {
            return Err(self.error(RuntimeKind::Register));
        };
        if !matches_type(&value, kind) {
            return Err(self.error(RuntimeKind::Register));
        }
        let before = self.registers.insert(register_id.to_owned(), value.clone());
        self.register_deltas.push(delta(
            CallbackDeltaOperation::Set,
            register_id,
            before,
            Some(value),
            id,
            predicates,
        ));
        Ok(Flow::Continue)
    }

    fn set_register_item(
        &mut self,
        register_id: &str,
        key: &CallbackExpression,
        expression: &CallbackExpression,
        id: &str,
        predicates: &[String],
    ) -> Result<Flow, Error> {
        let key = map_key(&self.value(key)?).map_err(|fault| self.fault(fault))?;
        let value = self.value(expression)?;
        let mut updated = self
            .registers
            .get(register_id)
            .and_then(Value::as_object)
            .cloned()
            .ok_or_else(|| self.error(RuntimeKind::Register))?;
        updated.insert(key, value);
        let updated = Value::Object(updated);
        let before = self
            .registers
            .insert(register_id.to_owned(), updated.clone());
        self.register_deltas.push(delta(
            CallbackDeltaOperation::Set,
            register_id,
            before,
            Some(updated),
            id,
            predicates,
        ));
        Ok(Flow::Continue)
    }

    fn set_custom(
        &mut self,
        key: &str,
        expression: &CallbackExpression,
        id: &str,
        predicates: &[String],
    ) -> Result<Flow, Error> {
        let value = self.value(expression)?;
        let Some(kind) = self.custom_types.get(key) else {
            return Err(self.error(RuntimeKind::Transaction));
        };
        if kind
            .as_ref()
            .is_some_and(|kind| !matches_type(&value, kind))
        {
            return Err(self.error(RuntimeKind::Transaction));
        }
        let before = self.custom.insert(key.to_owned(), value.clone());
        self.custom_deltas.push(delta(
            CallbackDeltaOperation::Set,
            key,
            before,
            Some(value),
            id,
            predicates,
        ));
        Ok(Flow::Continue)
    }

    fn delete_custom(&mut self, key: &str, id: &str, predicates: &[String]) -> Flow {
        let before = self.custom.remove(key);
        self.custom_deltas.push(delta(
            CallbackDeltaOperation::Delete,
            key,
            before,
            None,
            id,
            predicates,
        ));
        Flow::Continue
    }

    fn branch(
        &mut self,
        condition: &CallbackExpression,
        then: &[S],
        otherwise: &[S],
    ) -> Result<Flow, Error> {
        let condition = self.value(condition)?;
        self.execute(if truthy(&condition) { then } else { otherwise })
    }

    fn for_range(
        &mut self,
        target: &str,
        bounds: &[CallbackExpression],
        body: &[S],
    ) -> Result<Flow, Error> {
        if bounds.len() != 2 {
            return Err(self.error(RuntimeKind::Transaction));
        }
        let start = self.range_bound(&bounds[0])?;
        let stop = self.range_bound(&bounds[1])?;
        stop.checked_sub(start)
            .and_then(|value| usize::try_from(value).ok())
            .filter(|value| *value <= 4096)
            .ok_or_else(|| self.error(RuntimeKind::Transaction))?;
        let mut flow = Flow::Continue;
        for item in start..stop {
            self.locals
                .insert(target.to_owned(), Value::Number(item.into()));
            flow = self.execute(body)?;
            if !matches!(flow, Flow::Continue) {
                break;
            }
        }
        Ok(flow)
    }

    fn range_bound(&mut self, expression: &CallbackExpression) -> Result<i64, Error> {
        self.value(expression)?
            .as_i64()
            .ok_or_else(|| self.error(RuntimeKind::Transaction))
    }

    fn raise(
        &mut self,
        exception_class: &str,
        message: &CallbackExpression,
        id: &str,
    ) -> Result<Flow, Error> {
        let message = self
            .value(message)?
            .as_str()
            .map(ToOwned::to_owned)
            .ok_or_else(|| self.error(RuntimeKind::Transaction))?;
        Ok(Flow::Raised(CallbackProgramException {
            class: exception_class.to_owned(),
            diagnostic: message,
            instruction_id: id.to_owned(),
        }))
    }

    fn observe(
        &mut self,
        channel: &str,
        payload: &CallbackExpression,
        id: &str,
        predicates: &[String],
    ) -> Result<Flow, Error> {
        let payload = self.value(payload)?;
        if !payload.is_object() {
            return Err(self.error(RuntimeKind::Observation));
        }
        let bytes = serde_json::to_vec(&payload)
            .map_err(|_| self.error(RuntimeKind::Observation))?
            .len();
        self.observation_bytes = self
            .observation_bytes
            .checked_add(bytes)
            .ok_or_else(|| self.error(RuntimeKind::Observation))?;
        if self.observation_bytes > 65_536 {
            return Err(self.error(RuntimeKind::Observation));
        }
        let canonical_sha256 = canonical_callback_json_sha256(&payload)
            .map_err(|_| self.error(RuntimeKind::Observation))?;
        self.observations.push(CallbackObservation {
            channel: channel.to_owned(),
            payload,
            canonical_sha256,
            producer_instruction_id: id.to_owned(),
            predicate_ids: predicates.to_vec(),
        });
        Ok(Flow::Continue)
    }

    fn value(&mut self, value: &CallbackExpression) -> Result<Value, Error> {
        Values {
            invocation: self.invocation,
            registers: self.registers,
            custom: self.custom,
            locals: &self.locals,
        }
        .evaluate(value)
        .map_err(|fault| self.fault(fault))
    }

    fn return_value(
        &mut self,
        value: &CallbackReturn,
    ) -> Result<(CallbackReturnClass, Option<Value>), Error> {
        let result = value
            .value
            .as_ref()
            .map(|item| self.value(item))
            .transpose()?;
        Ok(normalize_return(value.class, result))
    }

    pub(super) fn fallback_value(
        &self,
        value: &CallbackFallback,
    ) -> Result<(CallbackReturnClass, Option<Value>), Error> {
        let result = if value.class == CallbackReturnClass::Stake
            && value.value.as_str() == Some("proposed_stake")
        {
            self.invocation
                .inputs
                .get("proposed_stake")
                .cloned()
                .ok_or_else(|| self.error(RuntimeKind::Missing))?
        } else {
            value.value.clone()
        };
        Ok((value.class, normalize_none(value.class, Some(result))))
    }

    fn step(&mut self) -> Result<(), Error> {
        self.remaining = self
            .remaining
            .checked_sub(1)
            .ok_or_else(|| self.error(RuntimeKind::Steps))?;
        Ok(())
    }

    fn fault(&self, fault: ValueFault) -> Error {
        match fault {
            ValueFault::Missing => self.error(RuntimeKind::Missing),
            ValueFault::Type | ValueFault::Arithmetic => self.error(RuntimeKind::Transaction),
        }
    }

    pub(super) fn error(&self, kind: RuntimeKind) -> Error {
        super::support::error(
            self.source_id,
            self.callback,
            self.current.clone(),
            self.timestamp_ms,
            kind,
        )
    }
}
